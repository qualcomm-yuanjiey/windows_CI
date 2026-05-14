"""
serial_handler.py
=================
Reads from a serial port in a background asyncio task and:
  1. Writes every line to a local log file on the Windows machine.
  2. Broadcasts each line to all connected serial-log TCP subscribers.

The SerialLogManager is started once when the bridge server starts and
runs for the lifetime of the process.  Clients connect to the dedicated
``serial_log_port`` and receive a continuous stream of newline-delimited
JSON objects:

    {"type": "serial_data", "timestamp": "...", "data": "<line>"}
    {"type": "serial_error", "timestamp": "...", "message": "<msg>"}
    {"type": "serial_status", "timestamp": "...", "message": "<msg>"}
"""

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("bridge.serial_handler")


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class SerialLogManager:
    """
    Thread-safe serial reader that fans out log lines to asyncio Queues.

    Architecture
    ------------
    * A dedicated daemon thread (``_reader_thread``) blocks on serial reads.
    * Each line is put onto a thread-safe ``threading.Queue`` (``_raw_queue``).
    * An asyncio task (``_dispatcher_task``) drains ``_raw_queue`` and pushes
      items into every subscriber's ``asyncio.Queue``.
    * TCP clients subscribe by calling ``subscribe()`` which returns an
      ``asyncio.Queue``; they unsubscribe via ``unsubscribe()``.
    """

    def __init__(self, cfg: dict):
        self._cfg = cfg["serial"]
        self._subscribers: list[asyncio.Queue] = []
        self._sub_lock = asyncio.Lock()
        self._raw_queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=5000)
        self._running = False
        self._reader_thread: threading.Thread | None = None
        self._dispatcher_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        log_path = Path(self._cfg.get("log_file", "serial_output.log"))
        self._log_fh = open(log_path, "a", encoding="utf-8", buffering=1)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self):
        """Start the serial reader thread and the asyncio dispatcher."""
        if self._running:
            log.warning("SerialLogManager already running")
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._dispatcher_task = asyncio.create_task(self._dispatcher())
        self._reader_thread = threading.Thread(
            target=self._reader_thread_fn, daemon=True, name="serial-reader"
        )
        self._reader_thread.start()
        log.info(
            "Serial log manager started – port=%s baud=%s",
            self._cfg["port"],
            self._cfg["baudrate"],
        )

    async def stop(self):
        """Gracefully stop the manager."""
        self._running = False
        if self._dispatcher_task:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass
        if self._log_fh:
            self._log_fh.close()
        log.info("Serial log manager stopped")

    async def subscribe(self) -> asyncio.Queue:
        """Return a new asyncio.Queue that will receive serial log dicts."""
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        async with self._sub_lock:
            self._subscribers.append(q)
        log.debug("New serial subscriber (total=%d)", len(self._subscribers))
        return q

    async def unsubscribe(self, q: asyncio.Queue):
        """Remove a subscriber queue."""
        async with self._sub_lock:
            self._subscribers = [s for s in self._subscribers if s is not q]
        log.debug("Serial subscriber removed (total=%d)", len(self._subscribers))

    # ------------------------------------------------------------------
    # Internal – reader thread (blocking I/O, runs in a daemon thread)
    # ------------------------------------------------------------------

    def _reader_thread_fn(self):
        """Blocking serial read loop – runs in a background thread."""
        try:
            import serial as pyserial  # noqa: PLC0415
        except ImportError:
            msg = {
                "type": "serial_error",
                "timestamp": _ts(),
                "message": "pyserial not installed – run: pip install pyserial",
            }
            self._enqueue(msg)
            log.error("pyserial not installed")
            return

        ser = None
        while self._running:
            try:
                if ser is None or not ser.is_open:
                    ser = pyserial.Serial(
                        port=self._cfg["port"],
                        baudrate=int(self._cfg["baudrate"]),
                        bytesize=int(self._cfg.get("bytesize", 8)),
                        parity=self._cfg.get("parity", "N"),
                        stopbits=int(self._cfg.get("stopbits", 1)),
                        timeout=float(self._cfg.get("timeout", 1)),
                    )
                    self._enqueue({
                        "type": "serial_status",
                        "timestamp": _ts(),
                        "message": f"Opened {self._cfg['port']} @ {self._cfg['baudrate']} baud",
                    })
                    log.info("Serial port %s opened", self._cfg["port"])

                raw = ser.readline()
                if not raw:
                    continue  # timeout – loop again

                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                ts = _ts()

                # Write to local log file (guard against closed file on shutdown)
                try:
                    self._log_fh.write(f"[{ts}] {line}\n")
                except ValueError:
                    pass  # file closed during shutdown – stop writing

                # Enqueue for broadcast
                self._enqueue({"type": "serial_data", "timestamp": ts, "data": line})

            except pyserial.SerialException as exc:
                log.warning("Serial error: %s – retrying in 3 s", exc)
                self._enqueue({
                    "type": "serial_error",
                    "timestamp": _ts(),
                    "message": str(exc),
                })
                if ser and ser.is_open:
                    try:
                        ser.close()
                    except Exception:  # noqa: BLE001
                        pass
                ser = None
                import time; time.sleep(3)  # noqa: E702

            except Exception as exc:  # noqa: BLE001
                log.exception("Unexpected error in serial reader")
                self._enqueue({
                    "type": "serial_error",
                    "timestamp": _ts(),
                    "message": str(exc),
                })
                import time; time.sleep(1)  # noqa: E702

        if ser and ser.is_open:
            ser.close()

    def _enqueue(self, item: dict):
        """Thread-safe enqueue into the asyncio queue via call_soon_threadsafe."""
        if self._loop and not self._loop.is_closed():
            def _put():
                try:
                    self._raw_queue.put_nowait(item)
                except asyncio.QueueFull:
                    # Drop the oldest item to make room, then insert the new one.
                    log.warning("Raw serial queue full – dropping oldest item")
                    try:
                        self._raw_queue.get_nowait()
                        self._raw_queue.put_nowait(item)
                    except Exception:  # noqa: BLE001
                        pass
            self._loop.call_soon_threadsafe(_put)

    # ------------------------------------------------------------------
    # Internal – asyncio dispatcher
    # ------------------------------------------------------------------

    async def _dispatcher(self):
        """Drain _raw_queue and fan out to all subscriber queues."""
        while self._running:
            try:
                item = await asyncio.wait_for(self._raw_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            async with self._sub_lock:
                subs = list(self._subscribers)

            for q in subs:
                try:
                    q.put_nowait(item)
                except asyncio.QueueFull:
                    log.warning("Subscriber queue full – dropping oldest item")
                    try:
                        q.get_nowait()
                        q.put_nowait(item)
                    except Exception:  # noqa: BLE001
                        pass
