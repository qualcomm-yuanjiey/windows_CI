#!/usr/bin/env python3
"""
sequence_runner.py
==================
Reads a sequence.yaml file and executes each step in order using
BridgeClient and SerialLogReceiver.

Usage
-----
    python client/sequence_runner.py --host 192.168.1.100

    # Custom sequence file:
    python client/sequence_runner.py --sequence client/sequence/base/sequence.yaml --host 192.168.1.100
"""

import argparse
import logging
import os
import sys
import subprocess
import threading
import time
from pathlib import Path

import yaml

# Allow running from repo root or client/ directory
sys.path.insert(0, str(Path(__file__).parent))

from linux_client import BridgeClient, BridgeClientError
from serial_log_receiver import SerialLogReceiver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("sequence_runner")


# ---------------------------------------------------------------------------
# Sequence runner
# ---------------------------------------------------------------------------

class SequenceRunner:
    """
    Executes steps defined in a sequence.yaml file.

    Supported actions
    -----------------
    read_uart     – start serial log capture in a background thread
    stop_uart     – stop capture and save log to file
    power_off     – TAC power off
    power_on      – TAC power on
    power_cycle   – TAC power cycle
    boot_edl      – TAC boot to EDL
    boot_fastboot – TAC boot to Fastboot
    boot_normal   – TAC boot to Normal
    boot_recovery – TAC boot to Recovery
    fastboot      – run fastboot command on Windows host
    fastboot_boot – fastboot boot <image> (no flash)
    adb           – run ADB command
    adb_push      – push file from Windows host to device
    adb_pull      – pull file from device to Windows host
    windows_shell – run command on Windows host (cmd.exe)
    wait          – sleep N seconds
    ping          – health-check bridge server
    linux_shell   – run command locally on Linux client (/bin/bash)
    """
    # NOTE: windows_shell → runs on the remote Windows host via bridge (cmd.exe)
    #       linux_shell  → runs locally on this Linux client via /bin/bash

    def __init__(self, host: str, command_port: int = 9000,
                 serial_log_port: int = 9001, auth_token: str = ""):
        self.host             = host
        self.command_port     = command_port
        self.serial_log_port  = serial_log_port
        self.auth_token       = auth_token

        # serial capture state
        self._capture_lines: list  = []
        self._capture_thread       = None
        self._capture_stop         = threading.Event()
        self._capture_output: str  = ""

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, sequence: list) -> bool:
        """
        Execute all steps in order.
        Returns True if all steps succeeded, False if any step aborted.
        """
        total = len(sequence)
        log.info("Starting sequence – %d step(s)", total)

        with BridgeClient(
            host=self.host,
            command_port=self.command_port,
            auth_token=self.auth_token,
            recv_timeout=300,
        ) as client:
            for idx, step in enumerate(sequence, 1):
                step_id = step.get("id", f"step_{idx}")
                action  = step.get("action", "").lower()
                params  = step.get("params", {}) or {}
                on_fail = step.get("on_fail", "abort").lower()

                log.info("─" * 50)
                log.info("[%d/%d] %s  action=%s", idx, total, step_id, action)

                try:
                    ok = self._dispatch(client, action, params)
                except Exception as exc:
                    log.error("Step '%s' raised exception: %s", step_id, exc)
                    ok = False

                if not ok:
                    log.error("Step '%s' FAILED (on_fail=%s)", step_id, on_fail)
                    if on_fail == "abort":
                        log.error("Aborting sequence.")
                        return False
                    else:
                        log.warning("Continuing despite failure.")
                else:
                    log.info("Step '%s' OK", step_id)

        log.info("─" * 50)
        log.info("Sequence complete.")
        return True

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    def _dispatch(self, client: BridgeClient, action: str, params: dict) -> bool:
        handlers = {
            "read_uart":     self._act_read_uart,
            "stop_uart":     self._act_stop_uart,
            "power_off":     self._act_power_off,
            "power_on":      self._act_power_on,
            "power_cycle":   self._act_power_cycle,
            "boot_edl":      self._act_boot_edl,
            "boot_fastboot": self._act_boot_fastboot,
            "boot_normal":   self._act_boot_normal,
            "boot_recovery": self._act_boot_recovery,
            "fastboot":      self._act_fastboot,
            "fastboot_boot": self._act_fastboot_boot,
            "adb":           self._act_adb,
            "adb_push":      self._act_adb_push,
            "adb_pull":      self._act_adb_pull,
            "windows_shell": self._act_windows_shell,
            "wait":          self._act_wait,
            "ping":          self._act_ping,
        }
        handlers["linux_shell"] = self._act_linux_shell
        fn = handlers.get(action)
        if fn is None:
            log.error("Unknown action: '%s'", action)
            return False
        return fn(client, params)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _check(self, result: dict, label: str,
               success_patterns: list = None,
               allowed_returncodes: list = None,
               ignore_stderr: bool = False) -> bool:
        """
        Return True if the step succeeded.

        Success is determined by:
          1. result["status"] == "ok"  (returncode == 0 from server)
          OR
          2. result["returncode"] in allowed_returncodes  (non-zero but acceptable)
          OR
          3. any string in success_patterns found in stdout or stderr

        stderr content alone is NOT treated as failure – many tools (emmcdl,
        fastboot, adb) print warnings/info to stderr even on success.
        """
        # ignore_stderr: treat step as OK regardless of returncode
        if ignore_stderr:
            status     = result.get("status", "error")
            returncode = result.get("returncode", -1)
            stdout     = result.get("stdout", "").strip()
            stderr     = result.get("stderr", "").strip()
            if stdout:
                log.info("%s stdout:\n%s", label, stdout)
            if stderr:
                log.info("%s stderr (ignored):\n%s", label, stderr)
            log.info("%s ignore_stderr=True – treating as OK (returncode=%s)", label, returncode)
            return True

        status     = result.get("status", "error")
        returncode = result.get("returncode", -1)
        stdout     = result.get("stdout", "").strip()
        stderr     = result.get("stderr", "").strip()

        if stdout:
            log.info("%s stdout:\n%s", label, stdout)
        if stderr:
            # log stderr as info – it is not necessarily an error
            log.info("%s stderr:\n%s", label, stderr)

        # check success_patterns in combined output
        if success_patterns:
            combined = stdout + "\n" + stderr
            for pattern in success_patterns:
                if pattern.lower() in combined.lower():
                    log.info("%s matched success_pattern '%s' – treating as OK", label, pattern)
                    return True

        # check allowed non-zero returncodes
        if allowed_returncodes and returncode in allowed_returncodes:
            log.info("%s returncode %d in allowed_returncodes – treating as OK", label, returncode)
            return True

        if status != "ok":
            log.error("%s FAILED (status=%s returncode=%s)", label, status, returncode)
            return False
        return True

    # ------------------------------------------------------------------
    # Serial log actions
    # ------------------------------------------------------------------

    def _act_read_uart(self, _client, params: dict) -> bool:
        """Start serial capture in a background thread."""
        if self._capture_thread and self._capture_thread.is_alive():
            log.warning("Serial capture already running – skipping read_uart")
            return True

        self._capture_output = params.get("output", "/tmp/serial_logs/capture.log")
        self._capture_lines  = []
        self._capture_stop.clear()

        def _capture():
            log.info("Serial capture started → %s", self._capture_output)
            while not self._capture_stop.is_set():
                rx = SerialLogReceiver(
                    host=self.host,
                    serial_log_port=self.serial_log_port,
                    auth_token=self.auth_token,
                    recv_timeout=30.0,
                )
                try:
                    rx.connect()
                except Exception as exc:
                    log.warning("Serial capture: connect failed (%s) – retrying in 3s", exc)
                    time.sleep(3)
                    continue
                try:
                    for entry in rx:
                        if self._capture_stop.is_set():
                            break
                        entry_type = entry.get("type", "")
                        ts         = entry.get("timestamp", "")
                        if entry_type == "serial_data":
                            line = entry.get("data", "")
                            self._capture_lines.append(f"[{ts}] {line}")
                            print(f"  UART: {line}")
                        elif entry_type in ("serial_error", "serial_status"):
                            msg = entry.get("message", "")
                            self._capture_lines.append(
                                f"[{ts}] *** {entry_type.upper()}: {msg} ***"
                            )
                except Exception as exc:
                    log.warning("Serial capture: stream error (%s) – reconnecting in 3s", exc)
                    time.sleep(3)
                finally:
                    rx.disconnect()
                if not self._capture_stop.is_set():
                    log.info("Serial capture: stream ended – reconnecting in 3s …")
                    time.sleep(3)

        self._capture_thread = threading.Thread(
            target=_capture, daemon=True, name="serial-capture"
        )
        self._capture_thread.start()
        time.sleep(1.0)   # let the thread connect before next step
        return True

    def _act_stop_uart(self, _client, params: dict) -> bool:
        """Wait for duration, stop capture, save log to file."""
        duration = float(params.get("duration", 60))
        output   = params.get("output", self._capture_output) or "/tmp/serial_logs/capture.log"

        if self._capture_thread and self._capture_thread.is_alive():
            log.info("Waiting %.0fs for serial log capture …", duration)
            try:
                self._capture_thread.join(timeout=duration)
            except KeyboardInterrupt:
                log.info("Capture interrupted by user")
            finally:
                self._capture_stop.set()
                self._capture_thread.join(timeout=5)

        # save
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(self._capture_lines) + "\n")
        log.info("Serial log saved → %s  (%d lines)", out_path, len(self._capture_lines))
        return True

    # ------------------------------------------------------------------
    # TAC power / boot actions
    # ------------------------------------------------------------------

    def _act_power_off(self, client: BridgeClient, params: dict) -> bool:
        return self._check(
            client.tac_power_off(sleep=params.get("sleep")), "power_off"
        )

    def _act_power_on(self, client: BridgeClient, params: dict) -> bool:
        return self._check(
            client.tac_power_on(sleep=params.get("sleep")), "power_on"
        )

    def _act_power_cycle(self, client: BridgeClient, params: dict) -> bool:
        return self._check(
            client.tac_power_cycle(sleep=params.get("sleep")), "power_cycle"
        )

    def _act_boot_edl(self, client: BridgeClient, params: dict) -> bool:
        return self._check(
            client.tac_boot_edl(sleep=params.get("sleep")), "boot_edl"
        )

    def _act_boot_fastboot(self, client: BridgeClient, params: dict) -> bool:
        return self._check(
            client.tac_boot_fastboot(sleep=params.get("sleep")), "boot_fastboot"
        )

    def _act_boot_normal(self, client: BridgeClient, params: dict) -> bool:
        return self._check(
            client.tac_boot_normal(sleep=params.get("sleep")), "boot_normal"
        )

    def _act_boot_recovery(self, client: BridgeClient, params: dict) -> bool:
        return self._check(
            client.tac_boot_recovery(sleep=params.get("sleep")), "boot_recovery"
        )

    # ------------------------------------------------------------------
    # Fastboot / ADB / shell actions
    # ------------------------------------------------------------------

    def _act_fastboot(self, client: BridgeClient, params: dict) -> bool:
        cmd = params.get("command", "").strip()
        if not cmd:
            log.error("fastboot action requires 'command' param")
            return False
        return self._check(
            client.fastboot(cmd,
                            serial=params.get("serial", ""),
                            timeout=int(params.get("timeout", 120))),
            f"fastboot {cmd}"
        )

    def _act_fastboot_boot(self, client: BridgeClient, params: dict) -> bool:
        image = params.get("image_path", "").strip()
        if not image:
            log.error("fastboot_boot action requires 'image_path' param")
            return False
        return self._check(
            client.fastboot_boot(image,
                                 serial=params.get("serial", ""),
                                 timeout=int(params.get("timeout", 120))),
            f"fastboot_boot {image}"
        )

    def _act_adb(self, client: BridgeClient, params: dict) -> bool:
        cmd = params.get("command", "").strip()
        if not cmd:
            log.error("adb action requires 'command' param")
            return False
        return self._check(
            client.adb(cmd,
                       serial=params.get("serial", ""),
                       timeout=int(params.get("timeout", 30))),
            f"adb {cmd}"
        )

    def _act_adb_push(self, client: BridgeClient, params: dict) -> bool:
        return self._check(
            client.adb_push(params["local"], params["remote"],
                            serial=params.get("serial", ""),
                            timeout=int(params.get("timeout", 60))),
            "adb_push"
        )

    def _act_adb_pull(self, client: BridgeClient, params: dict) -> bool:
        return self._check(
            client.adb_pull(params["remote"],
                            local=params.get("local", "."),
                            serial=params.get("serial", ""),
                            timeout=int(params.get("timeout", 60))),
            "adb_pull"
        )

    def _act_windows_shell(self, client: BridgeClient, params: dict) -> bool:
        cmd = params.get("command", "").strip()
        if not cmd:
            log.error("windows_shell action requires 'command' param")
            return False
        success_patterns    = params.get("success_patterns", None)
        allowed_returncodes = params.get("allowed_returncodes", None)
        ignore_stderr       = bool(params.get("ignore_stderr", False))
        cmd_timeout = int(params.get("timeout", 60))
        client.set_recv_timeout(cmd_timeout + 30)
        return self._check(
            client.shell(cmd, timeout=cmd_timeout),
            f"windows_shell {cmd}",
            success_patterns=success_patterns,
            allowed_returncodes=allowed_returncodes,
            ignore_stderr=ignore_stderr,
        )

    def _act_linux_shell(self, _client, params: dict) -> bool:
        """Run a command locally on the Linux client using /bin/bash."""
        cmd = params.get("command", "").strip()
        if not cmd:
            log.error("linux_shell action requires 'command' param")
            return False
        timeout = int(params.get("timeout", 60))
        log.info("linux_shell (local) running: %s", cmd)
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                executable="/bin/bash",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
            output = proc.stdout.decode(errors="replace").strip()
            if output:
                log.info("linux_shell stdout:\n%s", output)
            if proc.returncode != 0:
                log.error("linux_shell FAILED (returncode=%d)", proc.returncode)
                return False
            return True
        except subprocess.TimeoutExpired:
            log.error("linux_shell timed out after %ds", timeout)
            return False
        except Exception as exc:
            log.error("linux_shell exception: %s", exc)
            return False

    def _act_wait(self, _client, params: dict) -> bool:
        seconds = float(params.get("seconds", 5))
        log.info("Waiting %.1f seconds …", seconds)
        time.sleep(seconds)
        return True

    def _act_ping(self, client: BridgeClient, _params: dict) -> bool:
        return self._check(client.ping(), "ping")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_sequence(path: str) -> list:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    steps = data.get("sequence", [])
    if not steps:
        raise ValueError(f"No 'sequence' key found in {path}")
    return steps


def load_client_config(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Execute a step sequence defined in sequence.yaml"
    )
    parser.add_argument(
        "--sequence",
        default=str(Path(__file__).parent / "sequence" / "base" / "sequence.yaml"),
        help="Path to sequence.yaml (default: sequence.yaml in same directory)",
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.yaml"),
        help="Path to client config.yaml",
    )
    parser.add_argument(
        "--host",
        default="",
        help="Override bridge host IP (overrides config.yaml)",
    )
    args = parser.parse_args()

    # Load client config
    cfg        = load_client_config(args.config)
    bridge_cfg = cfg.get("bridge", {})

    host             = args.host or bridge_cfg.get("host", "192.168.1.100")
    command_port     = int(bridge_cfg.get("command_port", 9000))
    serial_log_port  = int(bridge_cfg.get("serial_log_port", 9001))
    auth_token       = bridge_cfg.get("auth_token", "")

    # Load sequence
    steps = load_sequence(args.sequence)
    log.info("Loaded %d step(s) from %s", len(steps), args.sequence)
    log.info("Bridge host: %s:%d", host, command_port)

    # Run
    runner = SequenceRunner(
        host=host,
        command_port=command_port,
        serial_log_port=serial_log_port,
        auth_token=auth_token,
    )
    success = runner.run(steps)
    sys.exit(0 if success else 1)
