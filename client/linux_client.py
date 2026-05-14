"""
linux_client.py
===============
Python client library for the Windows Bridge Server.

Runs on the remote Linux machine (where the LLM agent lives).
Provides a clean, synchronous API for:
  - Sending ADB / fastboot / shell commands to the Windows bridge
  - TACDev power control and boot mode operations
  - Receiving command results
  - Starting / stopping serial log streaming

Typical usage
-------------
    from linux_client import BridgeClient

    client = BridgeClient(host="192.168.1.100", command_port=9000)
    client.connect()

    # ADB
    result = client.adb("shell getprop ro.build.version.release")
    print(result["stdout"])

    # Fastboot
    result = client.fastboot("devices")
    print(result["stdout"])

    # TACDev – power cycle and boot to fastboot
    client.tac_power_cycle()
    client.tac_boot_fastboot()

    client.disconnect()

Thread safety
-------------
The client is NOT thread-safe by default.  If you need concurrent access,
create one BridgeClient instance per thread, or wrap calls with a lock.
"""

import json
import logging
import socket
import time
import uuid
from typing import Optional

log = logging.getLogger("bridge.client")


class BridgeClientError(Exception):
    """Raised when the bridge server returns an error or the connection fails."""


class BridgeClient:
    """
    Synchronous TCP client for the Windows Bridge Server command port.

    Parameters
    ----------
    host : str
        IP address or hostname of the Windows machine.
    command_port : int
        TCP port for the command channel (default 9000).
    auth_token : str
        Secret token if the server requires authentication (default "").
    connect_timeout : float
        Seconds to wait when establishing the TCP connection.
    recv_timeout : float
        Seconds to wait for a response from the server.
        TACDev power-cycle commands block for ~10 s (2 × 5 s sleep) on the
        server side, so this should be at least 30 s.
    auto_reconnect : bool
        Automatically reconnect on connection loss before retrying a command.
    """

    def __init__(
        self,
        host: str,
        command_port: int = 9000,
        auth_token: str = "",
        connect_timeout: float = 10.0,
        recv_timeout: float = 120.0,
        auto_reconnect: bool = True,
    ):
        self.host = host
        self.command_port = command_port
        self.auth_token = auth_token
        self.connect_timeout = connect_timeout
        self.recv_timeout = recv_timeout
        self.auto_reconnect = auto_reconnect

        self._sock: Optional[socket.socket] = None
        self._fh = None  # file-like wrapper for readline

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self):
        """Open the TCP connection and authenticate if required."""
        log.info("Connecting to %s:%d ...", self.host, self.command_port)
        self._sock = socket.create_connection(
            (self.host, self.command_port), timeout=self.connect_timeout
        )
        self._sock.settimeout(self.recv_timeout)
        self._fh = self._sock.makefile("r", encoding="utf-8")

        if self.auth_token:
            self._authenticate()

        log.info("Connected to bridge server at %s:%d", self.host, self.command_port)

    def disconnect(self):
        """Close the TCP connection."""
        if self._fh:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._fh = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            self._sock = None
        log.info("Disconnected from bridge server")

    def is_connected(self) -> bool:
        return self._sock is not None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def ping(self) -> dict:
        """Health-check the bridge server."""
        return self._send("ping", {})

    def list_devices(self) -> dict:
        """List ADB and fastboot devices visible to the Windows machine."""
        return self._send("list_devices", {})

    # ------------------------------------------------------------------
    # ADB
    # ------------------------------------------------------------------

    def adb(self, command: str, serial: str = "", timeout: int = 30) -> dict:
        """
        Run an ADB command on the Windows machine.

        Parameters
        ----------
        command : str
            ADB sub-command, e.g. ``"shell ls /"`` or ``"reboot bootloader"``.
        serial : str
            Override the configured ADB device serial (optional).
        timeout : int
            Command timeout in seconds.
        """
        return self._send("adb", {"command": command, "serial": serial, "timeout": timeout})

    def adb_push(self, local: str, remote: str, serial: str = "", timeout: int = 60) -> dict:
        """
        Push a file from the Windows machine to the device.

        ``local`` is a path on the **Windows** machine.
        """
        return self._send("adb_push", {"local": local, "remote": remote,
                                        "serial": serial, "timeout": timeout})

    def adb_pull(self, remote: str, local: str = ".", serial: str = "", timeout: int = 60) -> dict:
        """
        Pull a file from the device to the Windows machine.

        ``local`` is a destination path on the **Windows** machine.
        """
        return self._send("adb_pull", {"remote": remote, "local": local,
                                        "serial": serial, "timeout": timeout})

    # ------------------------------------------------------------------
    # Fastboot
    # ------------------------------------------------------------------

    def fastboot(self, command: str, serial: str = "", timeout: int = 120) -> dict:
        """
        Run a fastboot command on the Windows machine.

        Parameters
        ----------
        command : str
            Fastboot sub-command, e.g. ``"devices"``, ``"flash boot boot.img"``.
        serial : str
            Override the configured fastboot device serial (optional).
        timeout : int
            Command timeout in seconds.
        """
        return self._send("fastboot", {"command": command, "serial": serial, "timeout": timeout})

    def fastboot_boot(self, image_path: str, serial: str = "", timeout: int = 120) -> dict:
        """
        Boot a kernel image via ``fastboot boot <image_path>`` without flashing.

        The device must already be in fastboot/bootloader mode.

        Parameters
        ----------
        image_path : str
            Windows-accessible path to the boot image.
            Can be a local Windows path (e.g. ``"C:\\images\\boot.img"``) or a
            UNC network path (e.g. ``"\\\\fileserver\\share\\builds\\boot.img"``).
            The LLM agent supplies this path; the server executes the command.
        serial : str
            Override the configured fastboot device serial (optional).
        timeout : int
            Command timeout in seconds (default 120).
        """
        return self._send("fastboot_boot", {
            "image_path": image_path,
            "serial": serial,
            "timeout": timeout,
        })

    # ------------------------------------------------------------------
    # Windows host shell
    # ------------------------------------------------------------------

    def shell(self, command: str, timeout: int = 60) -> dict:
        """
        Run an arbitrary command on the Windows host (via cmd.exe).

        Parameters
        ----------
        command : str
            Windows shell command, e.g. ``"dir C:\\"`` or ``"ipconfig"``.
        timeout : int
            Command timeout in seconds.
        """
        return self._send("shell", {"command": command, "timeout": timeout})

    # ------------------------------------------------------------------
    # TACDev – helpers
    # ------------------------------------------------------------------

    def _tac_payload(
        self,
        vtp_number: Optional[str] = None,
        device_number: Optional[int] = None,
        sleep: Optional[float] = None,
        **extra,
    ) -> dict:
        """Build a TACDev payload, omitting fields that should use server defaults."""
        payload = dict(extra)
        if vtp_number is not None:
            payload["vtp_number"] = vtp_number
        if device_number is not None:
            payload["device_number"] = device_number
        if sleep is not None:
            payload["sleep"] = sleep
        return payload

    # ------------------------------------------------------------------
    # TACDev – info / status
    # ------------------------------------------------------------------

    def tac_list_devices(self) -> dict:
        """List all TAC devices (index, VTP number, port name, serial number)."""
        return self._send("tac_list_devices", {})

    def tac_get_versions(self) -> dict:
        """Return Alpaca and TAC library version strings."""
        return self._send("tac_get_versions", {})

    def tac_get_info(
        self,
        vtp_number: Optional[str] = None,
        device_number: Optional[int] = None,
    ) -> dict:
        """
        Get full info for the configured (or specified) TAC device.
        Returns VTP number, port name, serial number, battery state.
        """
        return self._send("tac_get_info",
                          self._tac_payload(vtp_number=vtp_number, device_number=device_number))

    def tac_get_status(
        self,
        vtp_number: Optional[str] = None,
        device_number: Optional[int] = None,
    ) -> dict:
        """
        Get runtime status: USB state, charging state, temperature, voltage, current.
        """
        return self._send("tac_get_status",
                          self._tac_payload(vtp_number=vtp_number, device_number=device_number))

    # ------------------------------------------------------------------
    # TACDev – power control
    # ------------------------------------------------------------------

    def tac_power_off(
        self,
        vtp_number: Optional[str] = None,
        device_number: Optional[int] = None,
        sleep: Optional[float] = None,
    ) -> dict:
        """
        Power off the TAC device.
        Server sleeps ``sleep`` seconds (default 5) after PowerOffButton().
        """
        return self._send("tac_power_off",
                          self._tac_payload(vtp_number=vtp_number,
                                            device_number=device_number, sleep=sleep))

    def tac_power_on(
        self,
        vtp_number: Optional[str] = None,
        device_number: Optional[int] = None,
        sleep: Optional[float] = None,
    ) -> dict:
        """
        Power on the TAC device.
        Server sleeps ``sleep`` seconds (default 5) after PowerOnButton().
        """
        return self._send("tac_power_on",
                          self._tac_payload(vtp_number=vtp_number,
                                            device_number=device_number, sleep=sleep))

    def tac_power_cycle(
        self,
        vtp_number: Optional[str] = None,
        device_number: Optional[int] = None,
        sleep: Optional[float] = None,
    ) -> dict:
        """
        Power cycle: PowerOffButton → sleep → PowerOnButton → sleep.
        Server sleeps ``sleep`` seconds after each step.
        """
        return self._send("tac_power_cycle",
                          self._tac_payload(vtp_number=vtp_number,
                                            device_number=device_number, sleep=sleep))

    def tac_reboot(
        self,
        vtp_number: Optional[str] = None,
        device_number: Optional[int] = None,
        sleep: Optional[float] = None,
    ) -> dict:
        """Alias for tac_power_cycle."""
        return self.tac_power_cycle(vtp_number=vtp_number,
                                    device_number=device_number, sleep=sleep)

    # ------------------------------------------------------------------
    # TACDev – boot mode control
    # ------------------------------------------------------------------

    def tac_boot_edl(
        self,
        vtp_number: Optional[str] = None,
        device_number: Optional[int] = None,
        sleep: Optional[float] = None,
    ) -> dict:
        """
        Boot to EDL (Emergency Download / 9008) mode via BootToEDLButton().
        Server sleeps ``sleep`` seconds (default 5) after the call.
        """
        return self._send("tac_boot_edl",
                          self._tac_payload(vtp_number=vtp_number,
                                            device_number=device_number, sleep=sleep))

    def tac_boot_fastboot(
        self,
        vtp_number: Optional[str] = None,
        device_number: Optional[int] = None,
        sleep: Optional[float] = None,
    ) -> dict:
        """
        Boot to Fastboot mode via BootToFastBootButton().
        Server sleeps ``sleep`` seconds (default 5) after the call.
        """
        return self._send("tac_boot_fastboot",
                          self._tac_payload(vtp_number=vtp_number,
                                            device_number=device_number, sleep=sleep))

    def tac_boot_uefi(
        self,
        vtp_number: Optional[str] = None,
        device_number: Optional[int] = None,
        sleep: Optional[float] = None,
    ) -> dict:
        """
        Boot to UEFI menu via BootToUEFIMenuButton().
        Server sleeps ``sleep`` seconds (default 5) after the call.
        """
        return self._send("tac_boot_uefi",
                          self._tac_payload(vtp_number=vtp_number,
                                            device_number=device_number, sleep=sleep))

    def tac_boot_secondary_edl(
        self,
        vtp_number: Optional[str] = None,
        device_number: Optional[int] = None,
        sleep: Optional[float] = None,
    ) -> dict:
        """
        Boot to secondary EDL mode via BootToSecondaryEDLButton().
        Server sleeps ``sleep`` seconds (default 5) after the call.
        """
        return self._send("tac_boot_secondary_edl",
                          self._tac_payload(vtp_number=vtp_number,
                                            device_number=device_number, sleep=sleep))

    def tac_boot_recovery(
        self,
        vtp_number: Optional[str] = None,
        device_number: Optional[int] = None,
        sleep: Optional[float] = None,
    ) -> dict:
        """
        Boot to Recovery mode via BootToRecoveryButton().
        Server sleeps ``sleep`` seconds (default 5) after the call.
        """
        return self._send("tac_boot_recovery",
                          self._tac_payload(vtp_number=vtp_number,
                                            device_number=device_number, sleep=sleep))

    def tac_boot_normal(
        self,
        vtp_number: Optional[str] = None,
        device_number: Optional[int] = None,
        sleep: Optional[float] = None,
    ) -> dict:
        """
        Boot to Normal (Android) mode via BootToNormalButton().
        Server sleeps ``sleep`` seconds (default 5) after the call.
        """
        return self._send("tac_boot_normal",
                          self._tac_payload(vtp_number=vtp_number,
                                            device_number=device_number, sleep=sleep))

    # ------------------------------------------------------------------
    # TACDev – USB control
    # ------------------------------------------------------------------

    def tac_set_usb(
        self,
        state: bool,
        vtp_number: Optional[str] = None,
        device_number: Optional[int] = None,
    ) -> dict:
        """
        Enable (state=True) or disable (state=False) the USB connection.
        """
        return self._send("tac_set_usb",
                          self._tac_payload(vtp_number=vtp_number,
                                            device_number=device_number, state=state))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _authenticate(self):
        """Send auth token and verify the server accepts it."""
        req = {"id": str(uuid.uuid4()), "type": "auth", "auth": self.auth_token, "payload": {}}
        self._write(req)
        resp = self._read()
        if resp.get("status") != "ok":
            raise BridgeClientError(f"Authentication failed: {resp.get('stderr', resp)}")
        log.debug("Authenticated with bridge server")

    def _send(self, req_type: str, payload: dict, retried: bool = False) -> dict:
        """Send a request and return the parsed response dict."""
        if not self.is_connected():
            if self.auto_reconnect and not retried:
                log.warning("Not connected – attempting reconnect ...")
                self.connect()
            else:
                raise BridgeClientError("Not connected to bridge server")

        req = {
            "id":      str(uuid.uuid4()),
            "type":    req_type,
            "auth":    self.auth_token,
            "payload": payload,
        }

        try:
            self._write(req)
            resp = self._read()
        except (OSError, socket.error) as exc:
            log.warning("Connection error: %s", exc)
            self.disconnect()
            if self.auto_reconnect and not retried:
                log.info("Reconnecting and retrying ...")
                time.sleep(1)
                return self._send(req_type, payload, retried=True)
            raise BridgeClientError(f"Connection lost: {exc}") from exc

        return resp

    def _write(self, obj: dict):
        """Serialise *obj* to JSON and send over the socket."""
        data = json.dumps(obj) + "\n"
        self._sock.sendall(data.encode("utf-8"))

    def _read(self) -> dict:
        """Read one newline-terminated JSON line from the server."""
        line = self._fh.readline()
        if not line:
            raise BridgeClientError("Server closed the connection")
        return json.loads(line.strip())
