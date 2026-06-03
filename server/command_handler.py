"""
command_handler.py
==================
Handles execution of ADB, fastboot, host-shell, and TACDev commands on the
Windows machine.

All public methods accept a ``payload`` dict and return a result dict that is
serialised to JSON and sent back to the Linux client.

Configuration keys used
-----------------------
  adb.path              – path to adb.exe
  adb.default_timeout   – default ADB command timeout (seconds)
  adb.device_serial     – default ADB device serial (empty = first device)

  fastboot.path             – path to fastboot.exe
  fastboot.default_timeout  – default fastboot command timeout (seconds)
  fastboot.device_serial    – default fastboot device serial (empty = first device)

  tacdev.*              – see dispatch_handler.py
"""

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import sys

log = logging.getLogger("bridge.command_handler")


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int) -> dict:
    """Execute *cmd* as a subprocess and return a normalised result dict."""
    log.debug("Executing: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        status = "ok" if result.returncode == 0 else "error"
        return {
            "status": status,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        log.warning("Command timed out: %s", " ".join(cmd))
        return {
            "status": "error",
            "returncode": -1,
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
        }
    except FileNotFoundError as exc:
        log.error("Executable not found: %s", exc)
        return {
            "status": "error",
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error running command")
        return {
            "status": "error",
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
        }


# ---------------------------------------------------------------------------
# CommandHandler
# ---------------------------------------------------------------------------

class CommandHandler:
    """
    Stateless handler – one instance shared across all client connections.

    Supported request types
    -----------------------
    ping              – health-check
    list_devices      – list ADB + fastboot devices
    adb               – run any adb command
    adb_push          – push a file to the device
    adb_pull          – pull a file from the device
    fastboot          – run any fastboot command
    shell             – run a command on the Windows host (cmd.exe)

    TACDev request types (delegated to DispatchHandler)
    --------------------------------------------------
    tac_list_devices    – list all TAC devices (index, VTP, port, serial)
    tac_get_versions    – get Alpaca / TAC library versions
    tac_get_info        – get info for the configured TAC device
    tac_get_status      – get USB state, charging, temperature, voltage, current
    tac_power_off       – power off the device              [sleep after]
    tac_power_on        – power on the device               [sleep after]
    tac_power_cycle     – power off → sleep → power on      [sleep after each]
    tac_reboot          – alias for tac_power_cycle
    tac_boot_edl        – boot to EDL mode                  [sleep after]
    tac_boot_fastboot   – boot to Fastboot mode             [sleep after]
    tac_boot_recovery   – boot to Recovery mode             [sleep after]
    tac_boot_normal     – boot to Normal (Android) mode     [sleep after]
    tac_set_usb         – enable or disable USB connection
    """

    def __init__(self, cfg: dict):
        self._adb = cfg["adb"]["path"]
        self._adb_timeout = int(cfg["adb"]["default_timeout"])
        self._adb_serial = cfg["adb"].get("device_serial", "").strip()

        self._fb = cfg["fastboot"]["path"]
        self._fb_timeout = int(cfg["fastboot"]["default_timeout"])
        self._fb_serial = cfg["fastboot"].get("device_serial", "").strip()

        # Lazy-import DispatchHandler so the server starts even if TACDev is absent
        sys.path.insert(0, str(Path(__file__).parent))
        from dispatch_handler import DispatchHandler  # noqa: PLC0415
        self._tac = DispatchHandler(cfg)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def dispatch(self, request: dict) -> dict:
        """Route an incoming request dict to the correct handler method."""
        req_type = request.get("type", "").lower()
        payload = request.get("payload", {})

        # TACDev requests are delegated to DispatchHandler
        if req_type.startswith("tac_"):
            return self._tac.dispatch(request)

        handlers = {
            "ping":           self.handle_ping,
            "list_devices":   self.handle_list_devices,
            "adb":            self.handle_adb,
            "adb_push":       self.handle_adb_push,
            "adb_pull":       self.handle_adb_pull,
            "fastboot":       self.handle_fastboot,
            "fastboot_boot":  self.handle_fastboot_boot,
            "shell":          self.handle_shell,
        }

        handler = handlers.get(req_type)
        if handler is None:
            return {
                "status": "error",
                "stderr": f"Unknown request type: '{req_type}'",
                "stdout": "",
                "returncode": -1,
            }

        try:
            return handler(payload)
        except Exception as exc:  # noqa: BLE001
            log.exception("Handler raised an exception for type '%s'", req_type)
            return {
                "status": "error",
                "stderr": str(exc),
                "stdout": "",
                "returncode": -1,
            }

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def handle_ping(self, _payload: dict) -> dict:
        return {
            "status": "ok",
            "message": "pong",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def handle_list_devices(self, _payload: dict) -> dict:
        adb_res = _run([self._adb, "devices"], 10)
        fb_res = _run([self._fb, "devices"], 10)
        return {
            "status": "ok",
            "adb_devices": adb_res.get("stdout", ""),
            "fastboot_devices": fb_res.get("stdout", ""),
            "adb_default_serial": self._adb_serial or "(any)",
            "fastboot_default_serial": self._fb_serial or "(any)",
        }

    # ------------------------------------------------------------------
    # ADB
    # ------------------------------------------------------------------

    def _adb_serial_args(self, override_serial: str) -> list[str]:
        """Return ['-s', serial] if a serial is set, else []."""
        serial = override_serial.strip() if override_serial else self._adb_serial
        return ["-s", serial] if serial else []

    def handle_adb(self, payload: dict) -> dict:
        """
        payload keys:
          command  (str, required) – e.g. "shell ls /"
          serial   (str, optional) – override the configured device serial
          timeout  (int, optional) – override default timeout
        """
        raw = payload.get("command", "").strip()
        if not raw:
            return {"status": "error", "stderr": "No ADB command provided", "stdout": "", "returncode": -1}

        serial_override = payload.get("serial", "")
        timeout = int(payload.get("timeout", self._adb_timeout))

        cmd = [self._adb] + self._adb_serial_args(serial_override) + raw.split()
        log.info("ADB ▶ %s", " ".join(cmd))
        return _run(cmd, timeout)

    def handle_adb_push(self, payload: dict) -> dict:
        """
        payload keys:
          local    (str) – path on the Windows machine
          remote   (str) – path on the device
          serial   (str, optional) – override the configured device serial
          timeout  (int, optional)
        """
        local = payload.get("local", "")
        remote = payload.get("remote", "")
        if not local or not remote:
            return {"status": "error", "stderr": "'local' and 'remote' are required", "stdout": "", "returncode": -1}

        serial_override = payload.get("serial", "")
        timeout = int(payload.get("timeout", self._adb_timeout))

        cmd = [self._adb] + self._adb_serial_args(serial_override) + ["push", local, remote]
        log.info("ADB push ▶ %s → %s", local, remote)
        return _run(cmd, timeout)

    def handle_adb_pull(self, payload: dict) -> dict:
        """
        payload keys:
          remote   (str) – path on the device
          local    (str) – destination on the Windows machine (default: current dir)
          serial   (str, optional) – override the configured device serial
          timeout  (int, optional)
        """
        remote = payload.get("remote", "")
        if not remote:
            return {"status": "error", "stderr": "'remote' path is required", "stdout": "", "returncode": -1}

        local = payload.get("local", ".")
        serial_override = payload.get("serial", "")
        timeout = int(payload.get("timeout", self._adb_timeout))

        cmd = [self._adb] + self._adb_serial_args(serial_override) + ["pull", remote, local]
        log.info("ADB pull ▶ %s → %s", remote, local)
        return _run(cmd, timeout)

    # ------------------------------------------------------------------
    # Fastboot
    # ------------------------------------------------------------------

    def _fb_serial_args(self, override_serial: str) -> list[str]:
        """Return ['-s', serial] if a serial is set, else []."""
        serial = override_serial.strip() if override_serial else self._fb_serial
        return ["-s", serial] if serial else []

    def handle_fastboot(self, payload: dict) -> dict:
        """
        payload keys:
          command  (str, required) – e.g. "flash boot boot.img"
          serial   (str, optional) – override the configured fastboot device serial
          timeout  (int, optional)
        """
        raw = payload.get("command", "").strip()
        if not raw:
            return {"status": "error", "stderr": "No fastboot command provided", "stdout": "", "returncode": -1}

        serial_override = payload.get("serial", "")
        timeout = int(payload.get("timeout", self._fb_timeout))

        cmd = [self._fb] + self._fb_serial_args(serial_override) + raw.split()
        log.info("Fastboot ▶ %s", " ".join(cmd))
        return _run(cmd, timeout)

    # ------------------------------------------------------------------
    # Fastboot boot (boot a kernel image without flashing)
    # ------------------------------------------------------------------

    def handle_fastboot_boot(self, payload: dict) -> dict:
        """
        Run ``fastboot boot <image_path>`` to temporarily boot a kernel image.

        The image_path is a Windows-accessible path (local or UNC network path).
        The LLM agent supplies the path; the server executes it.

        payload keys:
          image_path  (str, required) – path to the boot image, e.g.
                        "C:\\images\\boot.img"  or
                        "\\\\fileserver\\share\\builds\\latest\\boot.img"
          serial      (str, optional) – override the configured fastboot serial
          timeout     (int, optional) – default 120 s
        """
        image_path = payload.get("image_path", "").strip()
        if not image_path:
            return {
                "status": "error",
                "stderr": "'image_path' is required (Windows path or UNC network path to the boot image)",
                "stdout": "",
                "returncode": -1,
            }

        serial_override = payload.get("serial", "")
        timeout = int(payload.get("timeout", self._fb_timeout))

        cmd = [self._fb] + self._fb_serial_args(serial_override) + ["boot", image_path]
        log.info("Fastboot boot ▶ %s", " ".join(cmd))
        return _run(cmd, timeout)

    # ------------------------------------------------------------------
    # Host shell
    # ------------------------------------------------------------------

    def handle_shell(self, payload: dict) -> dict:
        """
        Run an arbitrary command on the Windows host via cmd.exe.

        payload keys:
          command  (str, required) – e.g. "dir C:\\"
          timeout  (int, optional)
        """
        raw = payload.get("command", "").strip()
        if not raw:
            return {"status": "error", "stderr": "No shell command provided", "stdout": "", "returncode": -1}

        timeout = int(payload.get("timeout", 60))
        log.info("Shell ▶ %s", raw)
        return _run(["cmd", "/c", raw], timeout)