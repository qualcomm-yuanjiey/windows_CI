"""
serial_log_receiver.py
======================
Connects to the Windows Bridge Server's serial-log port and receives the
continuous stream of serial-port log lines.

Can be used in three ways:

1. **Blocking iterator** – iterate over log lines in a for-loop.
2. **Background thread** – call ``start()`` and register a callback.
3. **Save to file** – call ``save_to_file()`` to capture logs to disk.

Usage examples
--------------
    # 1. Blocking iterator
    from serial_log_receiver import SerialLogReceiver
    with SerialLogReceiver("192.168.1.100") as rx:
        for entry in rx:
            print(entry["data"])

    # 2. Background thread with callback
    def on_line(entry):
        print(f"[{entry['timestamp']}] {entry.get('data', entry.get('message'))}")

    rx = SerialLogReceiver("192.168.1.100")
    rx.start(callback=on_line)
    time.sleep(30)
    rx.stop()

    # 3. Save to file
    rx = SerialLogReceiver("192.168.1.100")
    rx.save_to_file("/tmp/device_serial.log", duration=60)
"""

import json
import logging
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

log = logging.getLogger("bridge.serial_receiver")


class SerialLogReceiver:
    """
    TCP client for the Windows Bridge Server serial-log port.

    Parameters
    ----------
    host : str
        IP address or hostname of the Windows machine.
    serial_log_port : int
        TCP port for the serial log stream (default 9001).
    auth_token : str
        Secret token if the server requires authentication (default "").
    connect_timeout : float
        Seconds to wait when establishing the TCP connection.
    recv_timeout : float
        Seconds to wait for the next log line before raising a timeout.
        Set to ``None`` for no timeout (block forever).
    """

    def __init__(
        self,
        host: str,
        serial_log_port: int = 9001,
        auth_token: str = "",
        connect_timeout: float = 10.0,
        recv_timeout: Optional[float] = 120.0,
    ):
        self.host = host
        self.serial_log_port = serial_log_port
        self.auth_token = auth_token
        self.connect_timeout = connect_timeout
        self.recv_timeout = recv_timeout

        self._sock: Optional[socket.socket] = None
        self._fh = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self):
        """Open the TCP connection to the serial log port."""
        log.info("Connecting to serial log stream at %s:%d …", self.host, self.serial_log_port)
        self._sock = socket.create_connection(
            (self.host, self.serial_log_port), timeout=self.connect_timeout
        )
        self._sock.settimeout(self.recv_timeout)
        self._fh = self._sock.makefile("r", encoding="utf-8")

        if self.auth_token:
            self._authenticate()

        log.info("Serial log stream connected")

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
        log.info("Serial log stream disconnected")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

    # ------------------------------------------------------------------
    # Blocking iterator
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[dict]:
        """
        Yield parsed log entry dicts.  Skips keepalive messages.
        Raises ``StopIteration`` when the connection is closed.
        """
        if not self._fh:
            raise RuntimeError("Not connected – call connect() first")

        while True:
            try:
                line = self._fh.readline()
            except socket.timeout:
                log.warning("Serial log stream timed out waiting for data")
                continue
            except OSError:
                log.info("Serial log stream connection closed")
                return

            if not line:
                log.info("Serial log stream EOF")
                return

            try:
                entry = json.loads(line.strip())
            except json.JSONDecodeError:
                log.warning("Malformed JSON from serial log stream: %r", line)
                continue

            if entry.get("type") == "keepalive":
                continue

            yield entry

    # ------------------------------------------------------------------
    # Background thread mode
    # ------------------------------------------------------------------

    def start(self, callback: Callable[[dict], None]):
        """
        Start receiving serial logs in a background daemon thread.

        Parameters
        ----------
        callback : callable
            Called with each log entry dict as it arrives.
        """
        if self._thread and self._thread.is_alive():
            log.warning("Receiver already running")
            return

        self._stop_event.clear()
        self.connect()

        def _run():
            try:
                for entry in self:
                    if self._stop_event.is_set():
                        break
                    try:
                        callback(entry)
                    except Exception:  # noqa: BLE001
                        log.exception("Exception in serial log callback")
            finally:
                self.disconnect()

        self._thread = threading.Thread(target=_run, daemon=True, name="serial-log-rx")
        self._thread.start()
        log.info("Serial log receiver thread started")

    def stop(self, timeout: float = 5.0):
        """Stop the background receiver thread."""
        self._stop_event.set()
        self.disconnect()
        if self._thread:
            self._thread.join(timeout=timeout)
        log.info("Serial log receiver stopped")

    # ------------------------------------------------------------------
    # Save to file
    # ------------------------------------------------------------------

    def save_to_file(
        self,
        path: str,
        duration: Optional[float] = None,
        include_timestamps: bool = True,
        raw_only: bool = False,
    ):
        """
        Capture serial log lines to a file on the Linux machine.

        Parameters
        ----------
        path : str
            Destination file path on the Linux machine.
        duration : float or None
            How many seconds to capture.  ``None`` = capture until interrupted.
        include_timestamps : bool
            Prepend each line with an ISO-8601 timestamp.
        raw_only : bool
            If True, write only the raw ``data`` field (no JSON, no timestamps).
        """
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        log.info("Saving serial logs to %s (duration=%s s)", out_path, duration)
        start = time.monotonic()

        with open(out_path, "w", encoding="utf-8", buffering=1) as fh:
            self.connect()
            try:
                for entry in self:
                    if duration and (time.monotonic() - start) >= duration:
                        break

                    entry_type = entry.get("type", "")
                    if entry_type == "serial_data":
                        data = entry.get("data", "")
                        ts = entry.get("timestamp", "")
                        if raw_only:
                            fh.write(data + "\n")
                        elif include_timestamps:
                            fh.write(f"[{ts}] {data}\n")
                        else:
                            fh.write(data + "\n")
                    elif entry_type in ("serial_error", "serial_status"):
                        msg = entry.get("message", "")
                        ts = entry.get("timestamp", "")
                        if not raw_only:
                            fh.write(f"[{ts}] *** {entry_type.upper()}: {msg} ***\n")
            finally:
                self.disconnect()

        log.info("Serial log saved to %s", out_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _authenticate(self):
        """Send auth token and verify the server accepts it."""
        req = json.dumps({"auth": self.auth_token}) + "\n"
        self._sock.sendall(req.encode("utf-8"))
        line = self._fh.readline()
        resp = json.loads(line.strip())
        if resp.get("status") != "ok":
            raise ConnectionError(f"Serial log auth failed: {resp.get('message', resp)}")
        log.debug("Authenticated with serial log server")