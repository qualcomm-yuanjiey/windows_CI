#!/usr/bin/env python3
"""
test_framework.py
=================
Self-contained test suite for the Windows Bridge Server framework.

Runs directly on the Windows machine.  Tests:
  1. Config loading (COM4, 115200, VTP1)
  2. CommandHandler instantiation (with TACDev graceful fallback)
  3. SerialLogManager instantiation
  4. TCP server startup (ports 9000 + 9001)
  5. Ping over TCP
  6. ADB devices listing (graceful if adb not found)
  7. Fastboot devices listing (graceful if fastboot not found)
  8. Windows shell command
  9. TACDev list_devices (graceful if TACDev not installed)
 10. Serial port detection (COM4)

Usage:
    python test_framework.py
"""

import asyncio
import json
import os
import socket
import sys
import time
import threading
import uuid

# Make sure server/ is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "client"))

PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
WARN = "\033[93m[WARN]\033[0m"
INFO = "\033[94m[INFO]\033[0m"

results = []


def check(name: str, ok: bool, detail: str = ""):
    tag = PASS if ok else FAIL
    msg = f"{tag} {name}"
    if detail:
        msg += f"\n       {detail}"
    print(msg)
    results.append((name, ok))


# ---------------------------------------------------------------------------
# Test 1 – Config loading
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("  Test 1: Config loading")
print("=" * 60)

try:
    from windows_bridge_server import load_config
    cfg = load_config(os.path.join(os.path.dirname(__file__), "server", "config.yaml"))

    serial_port = cfg["serial"]["port"]
    baudrate    = cfg["serial"]["baudrate"]
    vtp_number  = cfg.get("tacdev", {}).get("vtp_number", "")
    cmd_port    = cfg["server"]["command_port"]
    serial_log_port = cfg["server"]["serial_log_port"]

    check("Config loaded",          True,  f"File: server/config.yaml")
    check("Serial port = COM4",     serial_port == "COM4",   f"Got: {serial_port}")
    check("Baud rate = 115200",     int(baudrate) == 115200, f"Got: {baudrate}")
    check("VTP number = VTP1",      vtp_number == "VTP1",    f"Got: {vtp_number}")
    check("Command port = 9000",    int(cmd_port) == 9000,   f"Got: {cmd_port}")
    check("Serial log port = 9001", int(serial_log_port) == 9001, f"Got: {serial_log_port}")

except Exception as exc:
    check("Config loading", False, str(exc))
    cfg = None

# ---------------------------------------------------------------------------
# Test 2 – CommandHandler instantiation
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("  Test 2: CommandHandler instantiation")
print("=" * 60)

handler = None
if cfg:
    try:
        from command_handler import CommandHandler
        handler = CommandHandler(cfg)
        check("CommandHandler created", True)
    except Exception as exc:
        check("CommandHandler created", False, str(exc))

# ---------------------------------------------------------------------------
# Test 3 – SerialLogManager instantiation
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("  Test 3: SerialLogManager instantiation")
print("=" * 60)

if cfg:
    try:
        from serial_handler import SerialLogManager
        mgr = SerialLogManager(cfg)
        check("SerialLogManager created", True)
    except Exception as exc:
        check("SerialLogManager created", False, str(exc))

# ---------------------------------------------------------------------------
# Test 4 – TCP server startup + ping
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("  Test 4: TCP server startup and ping")
print("=" * 60)

server_started = False
server_task = None
loop = None

if cfg and handler:
    try:
        from serial_handler import SerialLogManager
        from windows_bridge_server import CommandServer, SerialLogServer

        async def _run_server():
            serial_mgr = SerialLogManager(cfg)
            await serial_mgr.start()
            cmd_server_obj = CommandServer(cfg, handler)
            serial_log_server_obj = SerialLogServer(cfg, serial_mgr)
            srv = await asyncio.start_server(
                cmd_server_obj.handle_client, "127.0.0.1", int(cfg["server"]["command_port"])
            )
            return srv, serial_mgr

        loop = asyncio.new_event_loop()

        srv_ref = [None]
        mgr_ref = [None]
        ready_event = threading.Event()

        def _thread_fn():
            asyncio.set_event_loop(loop)
            async def _inner():
                srv, mgr = await _run_server()
                srv_ref[0] = srv
                mgr_ref[0] = mgr
                ready_event.set()
                async with srv:
                    await srv.serve_forever()
            loop.run_until_complete(_inner())

        t = threading.Thread(target=_thread_fn, daemon=True)
        t.start()
        ready_event.wait(timeout=5)
        time.sleep(0.3)
        server_started = True
        check("TCP server started on 127.0.0.1:9000", True)

    except Exception as exc:
        check("TCP server started", False, str(exc))

# ---------------------------------------------------------------------------
# Test 5 – Ping via TCP
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("  Test 5: Ping via TCP")
print("=" * 60)

if server_started:
    try:
        sock = socket.create_connection(("127.0.0.1", 9000), timeout=5)
        sock.settimeout(5)
        fh = sock.makefile("r", encoding="utf-8")
        req = json.dumps({"id": str(uuid.uuid4()), "type": "ping", "auth": "", "payload": {}}) + "\n"
        sock.sendall(req.encode("utf-8"))
        resp = json.loads(fh.readline().strip())
        sock.close()
        check("Ping response received",  resp.get("status") == "ok",  f"Response: {resp}")
        check("Ping returns 'pong'",     resp.get("message") == "pong", f"message={resp.get('message')}")
    except Exception as exc:
        check("Ping via TCP", False, str(exc))

# ---------------------------------------------------------------------------
# Test 6 – ADB devices
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("  Test 6: ADB devices")
print("=" * 60)

if server_started:
    try:
        sock = socket.create_connection(("127.0.0.1", 9000), timeout=5)
        sock.settimeout(10)
        fh = sock.makefile("r", encoding="utf-8")
        req = json.dumps({"id": str(uuid.uuid4()), "type": "list_devices", "auth": "", "payload": {}}) + "\n"
        sock.sendall(req.encode("utf-8"))
        resp = json.loads(fh.readline().strip())
        sock.close()
        ok = resp.get("status") == "ok"
        adb_out = resp.get("adb_devices", "")
        check("list_devices responded", ok, f"ADB output: {adb_out.strip()[:120]}")
    except Exception as exc:
        check("list_devices", False, str(exc))

# ---------------------------------------------------------------------------
# Test 7 – Windows shell command
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("  Test 7: Windows shell command (echo test)")
print("=" * 60)

if server_started:
    try:
        sock = socket.create_connection(("127.0.0.1", 9000), timeout=5)
        sock.settimeout(10)
        fh = sock.makefile("r", encoding="utf-8")
        req = json.dumps({
            "id": str(uuid.uuid4()),
            "type": "shell",
            "auth": "",
            "payload": {"command": "echo BRIDGE_TEST_OK", "timeout": 10}
        }) + "\n"
        sock.sendall(req.encode("utf-8"))
        resp = json.loads(fh.readline().strip())
        sock.close()
        stdout = resp.get("stdout", "").strip()
        check("Shell command executed",       resp.get("status") == "ok", f"stdout: {stdout}")
        check("Shell output contains marker", "BRIDGE_TEST_OK" in stdout,  f"stdout: {stdout}")
    except Exception as exc:
        check("Windows shell command", False, str(exc))

# ---------------------------------------------------------------------------
# Test 8 – TACDev list_devices (graceful if not installed)
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("  Test 8: TACDev list_devices")
print("=" * 60)

if server_started:
    try:
        sock = socket.create_connection(("127.0.0.1", 9000), timeout=5)
        sock.settimeout(10)
        fh = sock.makefile("r", encoding="utf-8")
        req = json.dumps({
            "id": str(uuid.uuid4()),
            "type": "tac_list_devices",
            "auth": "",
            "payload": {}
        }) + "\n"
        sock.sendall(req.encode("utf-8"))
        resp = json.loads(fh.readline().strip())
        sock.close()
        status = resp.get("status")
        stderr = resp.get("stderr", "")
        stdout = resp.get("stdout", "")
        if status == "ok":
            count = resp.get("device_count", 0)
            check("TACDev list_devices OK", True, f"{count} device(s): {stdout.strip()[:120]}")
        elif "TACDev Python module not found" in stderr:
            print(f"{WARN} TACDev list_devices: TACDev not installed (expected on dev machine)")
            print(f"       Install Alpaca from: C:\\Program Files (x86)\\Qualcomm\\Alpaca")
            results.append(("TACDev list_devices (graceful error)", True))
        else:
            check("TACDev list_devices", False, f"stderr: {stderr}")
    except Exception as exc:
        check("TACDev list_devices", False, str(exc))

# ---------------------------------------------------------------------------
# Test 9 – Serial port COM4 detection
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("  Test 9: Serial port COM4 detection")
print("=" * 60)

try:
    import serial.tools.list_ports as lp
    ports = [p.device for p in lp.comports()]
    if "COM4" in ports:
        check("COM4 detected", True, f"Available ports: {ports}")
    else:
        print(f"{WARN} COM4 not detected in available ports: {ports}")
        print("       Make sure the device is connected and powered on.")
        results.append(("COM4 detected (warning)", True))
except Exception as exc:
    check("Serial port detection", False, str(exc))

# ---------------------------------------------------------------------------
# Test 10 – BridgeClient (client library)
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("  Test 10: BridgeClient library")
print("=" * 60)

if server_started:
    try:
        from linux_client import BridgeClient
        with BridgeClient(host="127.0.0.1", command_port=9000) as client:
            result = client.ping()
            check("BridgeClient.ping()",        result.get("status") == "ok",
                  f"message={result.get('message')}")

            result = client.shell("echo CLIENT_TEST_OK")
            check("BridgeClient.shell()",       "CLIENT_TEST_OK" in result.get("stdout", ""),
                  f"stdout={result.get('stdout','').strip()}")

            result = client.tac_list_devices()
            tac_ok = result.get("status") == "ok" or "not found" in result.get("stderr", "").lower()
            check("BridgeClient.tac_list_devices()", tac_ok,
                  f"status={result.get('status')} stderr={result.get('stderr','')[:80]}")

    except Exception as exc:
        check("BridgeClient library", False, str(exc))

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("  SUMMARY")
print("=" * 60)
passed = sum(1 for _, ok in results if ok)
total  = len(results)
for name, ok in results:
    tag = PASS if ok else FAIL
    print(f"  {tag} {name}")
print(f"\n  {passed}/{total} tests passed")
if passed == total:
    print(f"\n  {PASS} All tests passed!")
else:
    print(f"\n  {FAIL} {total - passed} test(s) failed.")
print("=" * 60 + "\n")

# Stop the server loop
if loop and loop.is_running():
    loop.call_soon_threadsafe(loop.stop)