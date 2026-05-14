"""
tacdev_handler.py
=================
Handles TACDev power-control and boot-mode operations on the Windows machine.

TACDev is a Qualcomm library (``import TACDev``) for controlling TAC
(Test Automation Controller) devices connected via USB.

IMPORTANT: Every power-off, power-on, and boot-mode function call is followed
by a configurable sleep (default 5 seconds) to allow the device time to
transition states before the next command is issued.

Threading note
--------------
TACDev has Windows COM thread-affinity requirements: it must be called from
the same thread in which it was first used.  The bridge server dispatches
commands via asyncio's thread-pool executor, which uses different threads on
each call.  To work around this, all TACDev calls are serialised through a
single dedicated ``TACDevWorker`` daemon thread that owns the TACDev context
for the lifetime of the process.

Device identification
---------------------
The ``vtp_number`` config key is matched against:
  1. ``device.PortName()``   – e.g. "VTP1"
  2. ``device.SerialNumber()`` – e.g. "FTAEU329"
  3. Falls back to ``device_number`` index if no match is found.
"""

import logging
import queue
import threading
import time
from typing import Any

log = logging.getLogger("bridge.tacdev_handler")


# ---------------------------------------------------------------------------
# TACDev import guard
# ---------------------------------------------------------------------------

def _import_tacdev():
    try:
        import TACDev  # noqa: PLC0415
        return TACDev
    except ImportError as exc:
        raise ImportError(
            "TACDev Python module not found. "
            "Default install path: C:\\Program Files (x86)\\Qualcomm\\Alpaca"
        ) from exc


# ---------------------------------------------------------------------------
# Dedicated TACDev worker thread
# ---------------------------------------------------------------------------

class _TACDevWorker:
    """
    Runs all TACDev calls in a single dedicated daemon thread.

    This avoids the Windows COM thread-affinity crash that occurs when
    TACDev is called from asyncio's thread-pool executor threads.
    """

    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="tacdev-worker"
        )
        self._thread.start()

    def run(self, fn, *args, **kwargs) -> Any:
        """
        Submit *fn* to the worker thread and block until it returns.
        Raises any exception that *fn* raised.
        """
        result_holder = [None, None]  # [result, exception]
        done = threading.Event()

        def _task():
            try:
                result_holder[0] = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                result_holder[1] = exc
            finally:
                done.set()

        self._q.put(_task)
        done.wait()
        if result_holder[1] is not None:
            raise result_holder[1]
        return result_holder[0]

    def _loop(self):
        """Worker thread main loop."""
        while True:
            task = self._q.get()
            try:
                task()
            except Exception:  # noqa: BLE001
                pass  # exceptions are forwarded via result_holder


# Module-level singleton worker – created once, shared across all requests
_worker = _TACDevWorker()


# ---------------------------------------------------------------------------
# Context manager: open a TAC device, yield it, close it on exit
# (must be called from within the worker thread)
# ---------------------------------------------------------------------------

class _TACDeviceContext:
    def __init__(self, TACDev, device_number: int):
        self._TACDev = TACDev
        self._device_number = device_number
        self._device = None

    def __enter__(self):
        count = self._TACDev.GetDeviceCount()
        if count == 0:
            raise RuntimeError("No TAC devices found. Check USB connection.")
        if self._device_number < 0 or self._device_number >= count:
            raise ValueError(
                f"Device number {self._device_number} out of range (0-{count-1}). "
                "Use tac_list_devices to see available devices."
            )
        device = self._TACDev.GetDevice(self._device_number)
        if device is None:
            raise RuntimeError(f"TACDev.GetDevice({self._device_number}) returned None.")
        if not device.Open():
            raise RuntimeError(
                f"Cannot open TAC device {self._device_number} "
                f"(port: {device.PortName()}). Is another process using it?"
            )
        self._device = device
        return device

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._device is not None:
            try:
                self._device.Close()
            except Exception:  # noqa: BLE001
                log.warning("Exception closing TAC device %d", self._device_number)
        return False


# ---------------------------------------------------------------------------
# Helper: call a device method that may not exist on all firmware versions
# ---------------------------------------------------------------------------

def _safe_call(dev, method_name: str, *args, default="N/A"):
    """
    Call ``dev.<method_name>(*args)`` and return the result.
    Returns *default* if the method does not exist or raises an exception.
    This lets _get_status degrade gracefully on older TACDev firmware.
    """
    try:
        return getattr(dev, method_name)(*args)
    except Exception:  # noqa: BLE001
        return default


# ---------------------------------------------------------------------------
# TACDevHandler
# ---------------------------------------------------------------------------

class TACDevHandler:
    """
    Handles all TACDev-related requests.
    All TACDev calls are forwarded to the singleton _worker thread.
    """

    def __init__(self, cfg: dict):
        tac_cfg = cfg.get("tacdev", {})
        self._enabled       = bool(tac_cfg.get("enabled", True))
        self._vtp_number    = str(tac_cfg.get("vtp_number", "")).strip()
        self._device_number = int(tac_cfg.get("device_number", 0))
        self._sleep         = float(tac_cfg.get("post_action_sleep", 5))

    # ------------------------------------------------------------------
    # Helpers (run inside worker thread)
    # ------------------------------------------------------------------

    def _get_id(self, dev) -> str:
        """Return SerialNumber, falling back to PortName."""
        try:
            return str(dev.SerialNumber()).strip()
        except Exception:  # noqa: BLE001
            try:
                return str(dev.PortName()).strip()
            except Exception:  # noqa: BLE001
                return "N/A"

    def _resolve_device_number(self, TACDev, override_vtp: str = "", override_index: int = -1) -> int:
        """Match vtp_number against PortName or SerialNumber; fall back to index."""
        vtp = override_vtp.strip() if override_vtp else self._vtp_number
        if vtp:
            count = TACDev.GetDeviceCount()
            for i in range(count):
                dev = TACDev.GetDevice(i)
                if dev is not None:
                    try:
                        if str(dev.PortName()).strip() == vtp:
                            log.debug("Resolved '%s' via PortName -> index %d", vtp, i)
                            return i
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        if str(dev.SerialNumber()).strip() == vtp:
                            log.debug("Resolved '%s' via SerialNumber -> index %d", vtp, i)
                            return i
                    except Exception:  # noqa: BLE001
                        pass
            log.warning("'%s' not found among %d device(s) – using device_number=%d",
                        vtp, count, self._device_number)
        if override_index >= 0:
            return override_index
        return self._device_number

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def dispatch(self, request: dict) -> dict:
        if not self._enabled:
            return {"status": "error",
                    "stderr": "TACDev disabled in config (tacdev.enabled: false)",
                    "stdout": ""}

        req_type = request.get("type", "").lower()
        handlers = {
            "tac_list_devices":       self._list_devices,
            "tac_get_versions":       self._get_versions,
            "tac_get_info":           self._get_info,
            "tac_get_status":         self._get_status,
            "tac_power_off":          self._power_off,
            "tac_power_on":           self._power_on,
            "tac_power_cycle":        self._power_cycle,
            "tac_reboot":             self._power_cycle,
            "tac_boot_edl":           self._boot_edl,
            "tac_boot_fastboot":      self._boot_fastboot,
            "tac_boot_uefi":          self._boot_uefi,
            "tac_boot_secondary_edl": self._boot_secondary_edl,
            "tac_boot_recovery":      self._boot_recovery,
            "tac_boot_normal":        self._boot_normal,
            "tac_set_usb":            self._set_usb,
        }
        fn = handlers.get(req_type)
        if fn is None:
            return {"status": "error",
                    "stderr": f"Unknown TACDev request type: '{req_type}'",
                    "stdout": ""}
        try:
            # All TACDev calls go through the dedicated worker thread
            return _worker.run(fn, request.get("payload", {}))
        except Exception as exc:  # noqa: BLE001
            log.exception("TACDev error for '%s'", req_type)
            return {"status": "error", "stderr": str(exc), "stdout": ""}

    # ------------------------------------------------------------------
    # Implementations (executed inside worker thread)
    # ------------------------------------------------------------------

    def _list_devices(self, _payload: dict) -> dict:
        TACDev = _import_tacdev()
        count = TACDev.GetDeviceCount()
        devices = []
        for i in range(count):
            dev = TACDev.GetDevice(i)
            if dev is not None:
                devices.append({
                    "index":         i,
                    "port_name":     dev.PortName(),
                    "serial_number": self._get_id(dev),
                })
        log.info("TAC device list: %d device(s)", count)
        lines = [f"[{d['index']}] port={d['port_name']}  serial={d['serial_number']}"
                 for d in devices]
        return {"status": "ok", "device_count": count, "devices": devices,
                "stdout": "\n".join(lines) or "(no TAC devices found)", "stderr": ""}

    def _get_versions(self, _payload: dict) -> dict:
        TACDev = _import_tacdev()
        alpaca = TACDev.AlpacaVersion()
        tac    = TACDev.TACVersion()
        return {"status": "ok", "alpaca_version": str(alpaca), "tac_version": str(tac),
                "stdout": f"Alpaca: {alpaca}\nTAC: {tac}", "stderr": ""}

    def _get_info(self, payload: dict) -> dict:
        TACDev = _import_tacdev()
        dev_num = self._resolve_device_number(TACDev,
                    payload.get("vtp_number", ""), int(payload.get("device_number", -1)))
        with _TACDeviceContext(TACDev, dev_num) as dev:
            port    = dev.PortName()
            serial  = self._get_id(dev)
            battery = dev.GetBatteryState()
        return {"status": "ok", "device_number": dev_num, "port_name": port,
                "serial_number": serial, "battery_state": str(battery),
                "stdout": f"Device: {dev_num}\nPort: {port}\nSerial: {serial}\nBattery: {battery}",
                "stderr": ""}

    def _get_status(self, payload: dict) -> dict:
        TACDev = _import_tacdev()
        dev_num = self._resolve_device_number(TACDev,
                    payload.get("vtp_number", ""), int(payload.get("device_number", -1)))
        with _TACDeviceContext(TACDev, dev_num) as dev:
            port    = dev.PortName()
            serial  = self._get_id(dev)
            battery = dev.GetBatteryState()
            # Query extended status fields – wrapped individually so a missing
            # API method on older firmware doesn't fail the whole call.
            usb_state      = _safe_call(dev, "GetUSBState")
            charging_state = _safe_call(dev, "GetChargingState")
            temperature    = _safe_call(dev, "GetTemperature")
            voltage        = _safe_call(dev, "GetVoltage")
            current        = _safe_call(dev, "GetCurrent")
        lines = [
            f"Device: {dev_num}",
            f"Port: {port}",
            f"Serial: {serial}",
            f"Battery: {battery}",
            f"USB state: {usb_state}",
            f"Charging: {charging_state}",
            f"Temperature: {temperature}",
            f"Voltage: {voltage}",
            f"Current: {current}",
        ]
        return {
            "status":         "ok",
            "device_number":  dev_num,
            "port_name":      port,
            "serial_number":  serial,
            "battery_state":  str(battery),
            "usb_state":      str(usb_state),
            "charging_state": str(charging_state),
            "temperature":    str(temperature),
            "voltage":        str(voltage),
            "current":        str(current),
            "stdout":         "\n".join(lines),
            "stderr":         "",
        }

    def _power_action(self, payload: dict, action: str, method: str) -> dict:
        """Generic single-step power/boot action."""
        TACDev = _import_tacdev()
        dev_num = self._resolve_device_number(TACDev,
                    payload.get("vtp_number", ""), int(payload.get("device_number", -1)))
        sleep_s = float(payload.get("sleep", self._sleep))
        log.info("TAC %s – device %d (sleep %.1f s after)", action, dev_num, sleep_s)
        with _TACDeviceContext(TACDev, dev_num) as dev:
            port   = dev.PortName()
            serial = self._get_id(dev)
            getattr(dev, method)()
        log.info("%s() done – sleeping %.1f s", method, sleep_s)
        time.sleep(sleep_s)
        return {"status": "ok",
                "stdout": f"Device {dev_num} (port={port}, serial={serial}): {action} done. Slept {sleep_s}s.",
                "stderr": ""}

    def _power_off(self, payload: dict) -> dict:
        return self._power_action(payload, "power OFF", "PowerOffButton")

    def _power_on(self, payload: dict) -> dict:
        return self._power_action(payload, "power ON", "PowerOnButton")

    def _power_cycle(self, payload: dict) -> dict:
        TACDev = _import_tacdev()
        dev_num = self._resolve_device_number(TACDev,
                    payload.get("vtp_number", ""), int(payload.get("device_number", -1)))
        sleep_s = float(payload.get("sleep", self._sleep))
        log.info("TAC power CYCLE – device %d (sleep %.1f s after each step)", dev_num, sleep_s)
        with _TACDeviceContext(TACDev, dev_num) as dev:
            port   = dev.PortName()
            serial = self._get_id(dev)
            log.info("PowerOffButton() ...")
            dev.PowerOffButton()
        log.info("Powered off – sleeping %.1f s", sleep_s)
        time.sleep(sleep_s)
        with _TACDeviceContext(TACDev, dev_num) as dev:
            log.info("PowerOnButton() ...")
            dev.PowerOnButton()
        log.info("Powered on – sleeping %.1f s", sleep_s)
        time.sleep(sleep_s)
        return {"status": "ok",
                "stdout": (f"Device {dev_num} (port={port}, serial={serial}) power cycled. "
                           f"Slept {sleep_s}s after off and {sleep_s}s after on."),
                "stderr": ""}

    def _boot_edl(self, payload: dict) -> dict:
        return self._power_action(payload, "boot to EDL", "BootToEDLButton")

    def _boot_fastboot(self, payload: dict) -> dict:
        return self._power_action(payload, "boot to Fastboot", "BootToFastBootButton")

    def _boot_uefi(self, payload: dict) -> dict:
        return self._power_action(payload, "boot to UEFI", "BootToUEFIMenuButton")

    def _boot_secondary_edl(self, payload: dict) -> dict:
        return self._power_action(payload, "boot to secondary EDL", "BootToSecondaryEDLButton")

    def _boot_recovery(self, payload: dict) -> dict:
        TACDev = _import_tacdev()
        dev_num = self._resolve_device_number(TACDev,
                    payload.get("vtp_number", ""), int(payload.get("device_number", -1)))
        sleep_s = float(payload.get("sleep", self._sleep))
        with _TACDeviceContext(TACDev, dev_num) as dev:
            port   = dev.PortName()
            serial = self._get_id(dev)
            try:
                dev.SendCommand("recovery", True)
            except Exception:  # noqa: BLE001
                log.warning("SendCommand('recovery') unavailable – power cycling instead")
                dev.PowerOffButton()
                time.sleep(sleep_s)
                dev.PowerOnButton()
        time.sleep(sleep_s)
        return {"status": "ok",
                "stdout": f"Device {dev_num} (port={port}, serial={serial}): boot to Recovery. Slept {sleep_s}s.",
                "stderr": ""}

    def _boot_normal(self, payload: dict) -> dict:
        return self._power_action(payload, "boot to Normal", "PowerOnButton")

    def _set_usb(self, payload: dict) -> dict:
        raw = payload.get("state")
        if raw is None:
            return {"status": "error", "stderr": "'state' required (true/false)", "stdout": ""}
        TACDev = _import_tacdev()
        dev_num = self._resolve_device_number(TACDev,
                    payload.get("vtp_number", ""), int(payload.get("device_number", -1)))
        state = bool(raw)
        with _TACDeviceContext(TACDev, dev_num) as dev:
            port   = dev.PortName()
            serial = self._get_id(dev)
            dev.SetUSBState(state)
        label = "enabled" if state else "disabled"
        return {"status": "ok",
                "stdout": f"Device {dev_num} (port={port}, serial={serial}): USB {label}.",
                "stderr": ""}
