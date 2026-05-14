#!/usr/bin/env python3
"""
windows_bridge_server.py
========================
Main entry-point for the Windows Bridge Server.

Run on the Windows machine that has the embedded Linux device connected via USB.

Two TCP ports are opened:
  command_port    (default 9000) – request/response for ADB, fastboot, shell
  serial_log_port (default 9001) – continuous stream of serial-port log lines

Wire protocol (command port)
----------------------------
Every message is a newline-terminated JSON object.

Request  → {"id": "<uuid>", "type": "<type>", "auth": "<token>", "payload": {...}}
Response ← {"id": "<uuid>", "status": "ok|error", ...result fields...}

Wire protocol (serial log port)
--------------------------------
After the optional auth handshake the server streams newline-terminated JSON:
  {"type": "serial_data",   "timestamp": "...", "data": "<line>"}
  {"type": "serial_error",  "timestamp": "...", "message": "<msg>"}
  {"type": "serial_status", "timestamp": "...", "message": "<msg>"}

Usage
-----
    python windows_bridge_server.py [--config config.yaml]
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import yaml

# Local modules (same directory)
sys.path.insert(0, str(Path(__file__).parent))
from command_handler import CommandHandler
from serial_handler import SerialLogManager

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bridge_server.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("bridge_server")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    defaults = {
        "server": {
            "host": "0.0.0.0",
            "command_port": 9000,
            "serial_log_port": 9001,
            "auth_token": "",
            "max_clients": 5,
        },
        "adb": {"path": "adb", "default_timeout": 30},
        "fastboot": {"path": "fastboot", "default_timeout": 120},
        "serial": {
            "port": "COM3",
            "baudrate": 115200,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "timeout": 1,
            "log_file": "serial_output.log",
        },
    }
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            user = yaml.safe_load(fh) or {}
        for section, values in user.items():
            if section in defaults and isinstance(values, dict):
                defaults[section].update(values)
            else:
                defaults[section] = values
        log.info("Loaded config from %s", path)
    else:
        log.warning("Config file '%s' not found – using defaults", path)
    return defaults


# ---------------------------------------------------------------------------
# Command server (port 9000)
# ---------------------------------------------------------------------------

class CommandServer:
    """Handles one TCP client on the command port."""

    def __init__(self, cfg: dict, handler: CommandHandler):
        self._cfg = cfg
        self._handler = handler
        self._auth_token = cfg["server"].get("auth_token", "")
        self._active_clients = 0
        self._max_clients = int(cfg["server"].get("max_clients", 5))

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        peer = writer.get_extra_info("peername")
        log.info("Command client connected: %s", peer)

        if self._active_clients >= self._max_clients:
            log.warning("Max clients reached – rejecting %s", peer)
            writer.write(
                json.dumps({"status": "error", "stderr": "Server busy – max clients reached"}).encode()
                + b"\n"
            )
            await writer.drain()
            writer.close()
            return

        self._active_clients += 1
        authenticated = not bool(self._auth_token)  # skip auth if no token configured

        try:
            while True:
                try:
                    raw = await asyncio.wait_for(reader.readline(), timeout=300)
                except asyncio.TimeoutError:
                    log.info("Command client %s idle timeout", peer)
                    break

                if not raw:
                    log.info("Command client %s disconnected", peer)
                    break

                try:
                    request = json.loads(raw.decode("utf-8").strip())
                except json.JSONDecodeError as exc:
                    response = {"status": "error", "stderr": f"Invalid JSON: {exc}"}
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                    continue

                req_id = request.get("id", "")

                # ---- Authentication ----------------------------------------
                if not authenticated:
                    token = request.get("auth", "")
                    if token == self._auth_token:
                        authenticated = True
                        response = {"id": req_id, "status": "ok", "message": "Authenticated"}
                    else:
                        response = {"id": req_id, "status": "error", "stderr": "Authentication failed"}
                        writer.write(json.dumps(response).encode() + b"\n")
                        await writer.drain()
                        break
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                    continue

                # ---- Dispatch command ---------------------------------------
                log.info("Request from %s: type=%s id=%s", peer, request.get("type"), req_id)
                result = await asyncio.get_running_loop().run_in_executor(
                    None, self._handler.dispatch, request
                )
                result["id"] = req_id
                writer.write(json.dumps(result).encode() + b"\n")
                await writer.drain()

        except ConnectionResetError:
            log.info("Command client %s reset connection", peer)
        except Exception:  # noqa: BLE001
            log.exception("Error handling command client %s", peer)
        finally:
            self._active_clients -= 1
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            log.info("Command client %s disconnected (active=%d)", peer, self._active_clients)


# ---------------------------------------------------------------------------
# Serial log server (port 9001)
# ---------------------------------------------------------------------------

class SerialLogServer:
    """Streams serial log lines to connected TCP clients."""

    def __init__(self, cfg: dict, serial_mgr: SerialLogManager):
        self._cfg = cfg
        self._serial_mgr = serial_mgr
        self._auth_token = cfg["server"].get("auth_token", "")

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        peer = writer.get_extra_info("peername")
        log.info("Serial log client connected: %s", peer)

        # Optional auth handshake
        if self._auth_token:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=10)
                request = json.loads(raw.decode("utf-8").strip())
                if request.get("auth", "") != self._auth_token:
                    writer.write(
                        json.dumps({"status": "error", "message": "Authentication failed"}).encode() + b"\n"
                    )
                    await writer.drain()
                    writer.close()
                    return
                writer.write(json.dumps({"status": "ok", "message": "Authenticated"}).encode() + b"\n")
                await writer.drain()
            except Exception as exc:  # noqa: BLE001
                log.warning("Serial auth failed for %s: %s", peer, exc)
                writer.close()
                return

        # Subscribe to serial log stream
        queue = await self._serial_mgr.subscribe()
        log.info("Serial log client %s subscribed", peer)

        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=60)
                except asyncio.TimeoutError:
                    # Send a keepalive ping
                    writer.write(
                        json.dumps({"type": "keepalive", "message": "ping"}).encode() + b"\n"
                    )
                    await writer.drain()
                    continue

                writer.write(json.dumps(item).encode() + b"\n")
                await writer.drain()

        except (ConnectionResetError, BrokenPipeError):
            log.info("Serial log client %s disconnected", peer)
        except Exception:  # noqa: BLE001
            log.exception("Error streaming to serial log client %s", peer)
        finally:
            await self._serial_mgr.unsubscribe(queue)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            log.info("Serial log client %s cleaned up", peer)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(config_path: str):
    cfg = load_config(config_path)

    host = cfg["server"]["host"]
    cmd_port = int(cfg["server"]["command_port"])
    serial_port = int(cfg["server"]["serial_log_port"])

    # Instantiate shared components
    cmd_handler = CommandHandler(cfg)
    serial_mgr = SerialLogManager(cfg)
    cmd_server = CommandServer(cfg, cmd_handler)
    serial_log_server = SerialLogServer(cfg, serial_mgr)

    # Start serial reader
    await serial_mgr.start()

    # Start TCP servers
    command_srv = await asyncio.start_server(
        cmd_server.handle_client, host, cmd_port
    )
    serial_srv = await asyncio.start_server(
        serial_log_server.handle_client, host, serial_port
    )

    tac_cfg = cfg.get("tacdev", {})
    adb_serial = cfg["adb"].get("device_serial", "") or "(any)"
    fb_serial = cfg["fastboot"].get("device_serial", "") or "(any)"

    log.info("=" * 60)
    log.info("Windows Bridge Server started")
    log.info("  Command port      : %s:%d", host, cmd_port)
    log.info("  Serial log port   : %s:%d", host, serial_port)
    log.info("  Serial UART port  : %s @ %s baud", cfg["serial"]["port"], cfg["serial"]["baudrate"])
    log.info("  ADB device serial : %s", adb_serial)
    log.info("  Fastboot serial   : %s", fb_serial)
    log.info("  TACDev enabled    : %s", tac_cfg.get("enabled", True))
    log.info("  TACDev VTP number : %s", tac_cfg.get("vtp_number", "") or "(not set – using device index)")
    log.info("  TACDev device #   : %s", tac_cfg.get("device_number", 0))
    log.info("  TACDev sleep      : %s s", tac_cfg.get("post_action_sleep", 5))
    log.info("  Auth required     : %s", bool(cfg["server"].get("auth_token")))
    log.info("=" * 60)

    async with command_srv, serial_srv:
        try:
            await asyncio.gather(
                command_srv.serve_forever(),
                serial_srv.serve_forever(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await serial_mgr.stop()
            log.info("Bridge server shut down")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Windows Bridge Server for embedded device control")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.yaml"),
        help="Path to config.yaml (default: config.yaml in same directory)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(args.config))
    except KeyboardInterrupt:
        log.info("Interrupted by user")
