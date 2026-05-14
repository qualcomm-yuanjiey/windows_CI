#!/usr/bin/env python3
"""
reboot_and_capture_kernel_log.py
=================================
Uses the Windows Bridge framework (TCP API) to:
  1. Start the bridge server (command port 9000, serial log port 9001)
  2. Subscribe to the serial log stream BEFORE power cycle
  3. Power-cycle the device via TACDev (VTP1) using BridgeClient over TCP
  4. Capture ALL serial logs for up to 4 minutes (or until "Run /init")
  5. Save the log ONLY if "Booting Linux on physical CPU" is present
     (confirms the device actually booted Linux, not just UEFI/SBL)

Everything goes through the framework TCP API – no direct subprocess calls.

Usage:
    python reboot_and_capture_kernel_log.py
"""

import asyncio
import os
import sys
import threading
import time
import logging

# ── Path setup ──────────────────────────────────────────────────────────────
ROOT   = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(ROOT, "server")
CLIENT = os.path.join(ROOT, "client")
sys.path.insert(0, SERVER)
sys.path.insert(0, CLIENT)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("reboot_capture")

# ── Config ───────────────────────────────────────────────────────────────────
CONFIG_PATH     = os.path.join(SERVER, "config.yaml")
OUTPUT_FILE     = os.path.join(ROOT, "boot_kernel_log.txt")
CAPTURE_TIMEOUT = 240           # Max seconds to capture (4 minutes)
CMD_HOST        = "127.0.0.1"
CMD_PORT        = 9000
SERIAL_PORT     = 9001

# Capture until this appears (Linux kernel hands off to init)
STOP_KEYWORD    = "Run /init"

# Only save the log file if this marker is present (confirms Linux booted)
KERNEL_MARKER   = "Booting Linux on physical CPU"


# ============================================================================
# Step 1 – Start the bridge server in a background thread
# ============================================================================

def start_bridge_server(cfg: dict):
    """Start the asyncio bridge server in a daemon thread."""
    from windows_bridge_server import CommandServer, SerialLogServer
    from command_handler import CommandHandler
    from serial_handler import SerialLogManager

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run():
        asyncio.set_event_loop(loop)

        async def _inner():
            handler    = CommandHandler(cfg)
            serial_mgr = SerialLogManager(cfg)
            await serial_mgr.start()

            cmd_srv = CommandServer(cfg, handler)
            ser_srv = SerialLogServer(cfg, serial_mgr)

            cmd_server = await asyncio.start_server(
                cmd_srv.handle_client,
                CMD_HOST, int(cfg["server"]["command_port"])
            )
            serial_server = await asyncio.start_server(
                ser_srv.handle_client,
                CMD_HOST, int(cfg["server"]["serial_log_port"])
            )
            ready.set()
            async with cmd_server, serial_server:
                await asyncio.gather(
                    cmd_server.serve_forever(),
                    serial_server.serve_forever(),
                )

        loop.run_until_complete(_inner())

    t = threading.Thread(target=_run, daemon=True, name="bridge-server")
    t.start()
    ready.wait(timeout=10)
    time.sleep(0.8)
    log.info("Bridge server ready on %s:%d (cmd) / %d (serial)", CMD_HOST, CMD_PORT, SERIAL_PORT)
    return loop


# ============================================================================
# Step 2 – Start serial capture BEFORE power cycle
# ============================================================================

def start_serial_capture():
    """
    Connect to the serial log stream (TCP port 9001) and collect all lines
    in a background thread until STOP_KEYWORD or CAPTURE_TIMEOUT.
    Returns (lines_list, stop_event, found_kw, thread).
    """
    from serial_log_receiver import SerialLogReceiver

    lines      = []
    stop_event = threading.Event()
    found_kw   = [None]

    def _capture():
        rx = SerialLogReceiver(
            host=CMD_HOST,
            serial_log_port=SERIAL_PORT,
            recv_timeout=5.0,
        )
        rx.connect()
        log.info("Serial capture connected to %s:%d – waiting for boot output ...",
                 CMD_HOST, SERIAL_PORT)
        start = time.monotonic()
        try:
            for entry in rx:
                if stop_event.is_set():
                    break
                elapsed = time.monotonic() - start
                entry_type = entry.get("type", "")
                ts         = entry.get("timestamp", "")

                if entry_type == "serial_data":
                    line = entry.get("data", "")
                    lines.append(f"[{ts}] {line}")
                    print(f"  {line}")

                    if STOP_KEYWORD.lower() in line.lower():
                        found_kw[0] = STOP_KEYWORD
                        log.info("Stop keyword '%s' detected after %.1f s",
                                 STOP_KEYWORD, elapsed)
                        stop_event.set()
                        break

                elif entry_type in ("serial_error", "serial_status"):
                    msg = entry.get("message", "")
                    lines.append(f"[{ts}] *** {entry_type.upper()}: {msg} ***")
                    log.info("Serial %s: %s", entry_type, msg)

                if elapsed >= CAPTURE_TIMEOUT:
                    log.warning("Capture timeout (%ds) reached", CAPTURE_TIMEOUT)
                    stop_event.set()
                    break
        finally:
            rx.disconnect()
            log.info("Serial capture stopped – %d lines collected", len(lines))

    t = threading.Thread(target=_capture, daemon=True, name="serial-capture")
    t.start()
    time.sleep(1.0)   # let the capture thread connect before power cycle
    return lines, stop_event, found_kw, t


# ============================================================================
# Step 3 – Power-cycle via BridgeClient (TCP port 9000)
# ============================================================================

def power_cycle_device(cfg: dict) -> bool:
    """Use BridgeClient over TCP to power-cycle the device via TACDev."""
    from linux_client import BridgeClient

    vtp     = cfg.get("tacdev", {}).get("vtp_number", "")
    sleep_s = float(cfg.get("tacdev", {}).get("post_action_sleep", 5))

    log.info("Connecting to command port %s:%d ...", CMD_HOST, CMD_PORT)
    with BridgeClient(host=CMD_HOST, command_port=CMD_PORT, recv_timeout=60) as client:

        # ── ping ──────────────────────────────────────────────────────────
        ping = client.ping()
        log.info("Ping response: %s", ping.get("message"))

        # ── list TAC devices ──────────────────────────────────────────────
        devices = client.tac_list_devices()
        log.info("TAC devices:\n%s", devices.get("stdout", "").strip())

        # ── power cycle ───────────────────────────────────────────────────
        log.info("Calling tac_power_cycle(vtp_number='%s', sleep=%.0fs) ...", vtp, sleep_s)
        result = client.tac_power_cycle(vtp_number=vtp if vtp else None)
        if result.get("status") == "ok":
            log.info("Power cycle OK: %s", result.get("stdout", "").strip())
            return True
        else:
            log.error("Power cycle FAILED: %s", result.get("stderr", ""))
            return False


# ============================================================================
# Main
# ============================================================================

def main():
    print("\n" + "=" * 65)
    print("  Reboot & Boot Log Capture  (via Windows Bridge Framework)")
    print("=" * 65 + "\n")

    # Load config
    from windows_bridge_server import load_config
    cfg = load_config(CONFIG_PATH)
    log.info("Config: serial=%s@%s  VTP=%s  sleep=%ss",
             cfg["serial"]["port"], cfg["serial"]["baudrate"],
             cfg.get("tacdev", {}).get("vtp_number", ""),
             cfg.get("tacdev", {}).get("post_action_sleep", 5))

    # Start bridge server
    start_bridge_server(cfg)

    # ── Subscribe to serial stream BEFORE power cycle ─────────────────────
    log.info("Subscribing to serial log stream (port %d) BEFORE power cycle ...", SERIAL_PORT)
    lines, stop_event, found_kw, capture_thread = start_serial_capture()

    # ── Power cycle via TCP command port ──────────────────────────────────
    ok = power_cycle_device(cfg)
    if not ok:
        log.error("Aborting – power cycle failed.")
        stop_event.set()
        sys.exit(1)

    log.info("Power cycle done – capturing boot log (up to %ds) ...", CAPTURE_TIMEOUT)

    # Wait for capture thread to finish
    capture_thread.join(timeout=CAPTURE_TIMEOUT + 15)

    # ── Check for Linux kernel marker before saving ───────────────────────
    kernel_lines = [l for l in lines if KERNEL_MARKER in l]

    print("\n" + "=" * 65)
    log.info("Total lines captured : %d", len(lines))
    log.info("Stop reason          : %s", found_kw[0] or "timeout")
    log.info("Kernel marker found  : %s", "YES" if kernel_lines else "NO")

    if not kernel_lines:
        log.warning(
            "KERNEL MARKER '%s' NOT FOUND in captured log.", KERNEL_MARKER
        )
        log.warning(
            "Log file NOT saved – device did not appear to boot Linux "
            "(only XBL/SBL/UEFI output was seen)."
        )
        print("=" * 65 + "\n")
        return

    # ── Save log file ─────────────────────────────────────────────────────
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        fh.write("# Boot log captured via Windows Bridge Framework\n")
        fh.write(
            f"# Device: VTP={cfg.get('tacdev',{}).get('vtp_number','')}  "
            f"Serial: {cfg['serial']['port']} @ {cfg['serial']['baudrate']} baud\n"
        )
        fh.write(f"# Stop keyword : {found_kw[0] or '(timeout)'}\n")
        fh.write(f"# Total lines  : {len(lines)}\n")
        fh.write(f"# Kernel marker: {kernel_lines[0].strip()}\n\n")
        fh.write("\n".join(lines))
        fh.write("\n")

    log.info("Log saved to         : %s", OUTPUT_FILE)
    log.info("Kernel boot line     : %s", kernel_lines[0].strip())
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()