"""
dispatch_handler.py
=================
Handles all tac_* request types by delegating directly to TacDebugBoard
from debugboard.py (same directory).  No TACDev library calls here.

Supported request types
-----------------------
  tac_power_off          – power off the board
  tac_power_on           – power on the board
  tac_power_cycle        – power off -> sleep -> power on
  tac_reboot             – alias for tac_power_cycle
  tac_boot_edl           – boot to EDL mode
  tac_boot_fastboot      – boot to Fastboot mode
  tac_boot_normal        – normal power on (same as tac_power_on)
  tac_set_usb            – enable / disable USB connection

Not supported via debugboard.py (no equivalent API):
  tac_list_devices, tac_get_versions, tac_get_info,
  tac_get_status, tac_boot_uefi, tac_boot_secondary_edl,
  tac_boot_recovery
  → these return a clear "not supported" error.

Serial number
-------------
The TAC device serial number is resolved in this order:
  1. request payload["vtp_number"]
  2. config.yaml  tacdev.vtp_number
"""

import logging
import sys
import time
from pathlib import Path

log = logging.getLogger("bridge.dispatch_handler")

# Import TacDebugBoard from debugboard.py (same server/ directory)
sys.path.insert(0, str(Path(__file__).parent))
from debugboard import TacDebugBoard  # noqa: E402


# ---------------------------------------------------------------------------
# DispatchHandler
# ---------------------------------------------------------------------------

class DispatchHandler:
    """Dispatch tac_* requests using TacDebugBoard from debugboard.py."""

    def __init__(self, cfg: dict):
        tac_cfg         = cfg.get("tacdev", {})
        self._enabled   = bool(tac_cfg.get("enabled", True))
        self._serial    = str(tac_cfg.get("vtp_number", "")).strip()
        self._sleep     = float(tac_cfg.get("post_action_sleep", 5))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_serial(self, payload: dict) -> str:
        """Return serial from payload first, then config. Raise if missing."""
        serial = (payload.get("vtp_number") or self._serial or "").strip()
        if not serial:
            raise ValueError(
                "TAC device serial number is required. "
                "Set tacdev.vtp_number in config.yaml or pass vtp_number in the request."
            )
        return serial

    def _open(self, serial: str) -> TacDebugBoard:
        """Create and open a TacDebugBoard for the given serial number."""
        board = TacDebugBoard()
        board.open_connection(serial)
        return board

    def _ok(self, msg: str) -> dict:
        return {"status": "ok", "stdout": msg, "stderr": ""}

    def _err(self, msg: str) -> dict:
        return {"status": "error", "stdout": "", "stderr": msg}

    def _not_supported(self, req_type: str) -> dict:
        return self._err(
            f"'{req_type}' is not supported via debugboard.py. "
            "Use the TACDev library directly for info/status queries."
        )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def dispatch(self, request: dict) -> dict:
        if not self._enabled:
            return self._err("TACDev disabled in config (tacdev.enabled: false)")

        req_type = request.get("type", "").lower()
        payload  = request.get("payload", {})

        handlers = {
            "tac_list_devices":       self._list_devices,
            "tac_power_off":          self._power_off,
            "tac_power_on":           self._power_on,
            "tac_power_cycle":        self._power_cycle,
            "tac_reboot":             self._power_cycle,
            "tac_boot_edl":           self._boot_edl,
            "tac_boot_fastboot":      self._boot_fastboot,
            "tac_boot_normal":        self._power_on,
            "tac_set_usb":            self._set_usb,
        }

        not_supported = {
            "tac_get_versions", "tac_get_info",
            "tac_get_status", "tac_boot_uefi", "tac_boot_secondary_edl",
            "tac_boot_recovery",
        }

        if req_type in not_supported:
            return self._not_supported(req_type)

        fn = handlers.get(req_type)
        if fn is None:
            return self._err(f"Unknown TACDev request type: '{req_type}'")

        try:
            return fn(payload)
        except Exception as exc:  # noqa: BLE001
            log.exception("TACDev error for '%s'", req_type)
            return self._err(str(exc))

    # ------------------------------------------------------------------
    # Action implementations
    # ------------------------------------------------------------------

    def _list_devices(self, payload: dict) -> dict:
        log.info("TAC list devices")
        board = TacDebugBoard()
        devices = board.list_devices()
        lines = [
            f"[{d['index']}] serial={d['serial_number']}  port={d['port_name']}"
            for d in devices
        ]
        return {
            "status":       "ok",
            "device_count": len(devices),
            "devices":      devices,
            "stdout":       "\n".join(lines) if lines else "(no TAC devices found)",
            "stderr":       "",
        }

    def _power_off(self, payload: dict) -> dict:
        serial  = self._resolve_serial(payload)
        sleep_s = float(payload.get("sleep", self._sleep))
        log.info("TAC power OFF – serial=%s", serial)
        board = self._open(serial)
        try:
            board.set_power(False)      # PowerOffButton + sleep(2) inside
            board.set_usb(False)
        finally:
            board.close_connection()
        time.sleep(sleep_s)
        return self._ok(f"Serial {serial}: power OFF done. Slept {sleep_s}s.")

    def _power_on(self, payload: dict) -> dict:
        serial  = self._resolve_serial(payload)
        sleep_s = float(payload.get("sleep", self._sleep))
        log.info("TAC power ON – serial=%s", serial)
        board = self._open(serial)
        try:
            board.set_power(True)       # PowerOnButton + sleep(4) inside
            board.set_usb(True)
        finally:
            board.close_connection()
        time.sleep(sleep_s)
        return self._ok(f"Serial {serial}: power ON done. Slept {sleep_s}s.")

    def _power_cycle(self, payload: dict) -> dict:
        serial  = self._resolve_serial(payload)
        sleep_s = float(payload.get("sleep", self._sleep))
        log.info("TAC power CYCLE – serial=%s", serial)

        # power off
        board = self._open(serial)
        try:
            board.set_power(False)
            board.set_usb(False)
        finally:
            board.close_connection()

        log.info("Powered off – sleeping %.1f s", sleep_s)
        time.sleep(sleep_s)

        # power on
        board = self._open(serial)
        try:
            board.set_power(True)
            board.set_usb(True)
        finally:
            board.close_connection()

        log.info("Powered on – sleeping %.1f s", sleep_s)
        time.sleep(sleep_s)
        return self._ok(
            f"Serial {serial} power cycled. "
            f"Slept {sleep_s}s after off and {sleep_s}s after on."
        )

    def _boot_edl(self, payload: dict) -> dict:
        serial  = self._resolve_serial(payload)
        sleep_s = float(payload.get("sleep", self._sleep))
        log.info("TAC boot EDL – serial=%s", serial)
        board = self._open(serial)
        try:
            board.set_qdl(True)         # BootToEDLButton + sleep(2) inside
        finally:
            board.close_connection()
        time.sleep(sleep_s)
        return self._ok(f"Serial {serial}: boot to EDL done. Slept {sleep_s}s.")

    def _boot_fastboot(self, payload: dict) -> dict:
        serial  = self._resolve_serial(payload)
        sleep_s = float(payload.get("sleep", self._sleep))
        log.info("TAC boot fastboot – serial=%s", serial)
        board = self._open(serial)
        try:
            board.set_fastboot(True)    # BootToFastBootButton + sleep(2) inside
        finally:
            board.close_connection()
        time.sleep(sleep_s)
        return self._ok(f"Serial {serial}: boot to fastboot done. Slept {sleep_s}s.")

    def _set_usb(self, payload: dict) -> dict:
        raw = payload.get("state")
        if raw is None:
            return self._err("'state' is required (true/false)")
        serial = self._resolve_serial(payload)
        state  = bool(raw)
        log.info("TAC set USB %s – serial=%s", "ON" if state else "OFF", serial)
        board = self._open(serial)
        try:
            board.set_usb(state)
        finally:
            board.close_connection()
        label = "enabled" if state else "disabled"
        return self._ok(f"Serial {serial}: USB {label}.")
