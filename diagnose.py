#!/usr/bin/env python3
"""
diagnose.py
===========
Diagnostic script to verify:
  1. COM4 is accessible and receiving data
  2. TACDev can open the device from the main thread
  3. What data (if any) is coming from COM4

Run this BEFORE reboot_and_capture_kernel_log.py to confirm the setup.
"""

import os
import sys
import time
import threading

ROOT   = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(ROOT, "server")
CLIENT = os.path.join(ROOT, "client")
sys.path.insert(0, SERVER)
sys.path.insert(0, CLIENT)

PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
WARN = "\033[93m[WARN]\033[0m"
INFO = "\033[94m[INFO]\033[0m"

# ── 1. List available COM ports ──────────────────────────────────────────────
print("\n" + "=" * 60)
print("  1. Available COM ports")
print("=" * 60)
try:
    import serial.tools.list_ports as lp
    ports = list(lp.comports())
    for p in ports:
        print(f"  {p.device:10s}  {p.description}")
    if not ports:
        print("  (no COM ports found)")
except Exception as e:
    print(f"  ERROR: {e}")

# ── 2. Try to open COM4 and read for 5 seconds ───────────────────────────────
print("\n" + "=" * 60)
print("  2. Direct COM4 read test (5 seconds)")
print("=" * 60)
try:
    import serial
    print(f"  Opening COM4 @ 115200 baud ...")
    ser = serial.Serial("COM4", baudrate=115200, timeout=1)
    print(f"  {PASS} COM4 opened successfully")
    print(f"  Reading for 5 seconds (device must be powered on and outputting serial)...")
    lines_read = 0
    start = time.monotonic()
    while time.monotonic() - start < 5:
        raw = ser.readline()
        if raw:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            print(f"  RAW: {repr(line)}")
            lines_read += 1
    ser.close()
    if lines_read > 0:
        print(f"  {PASS} Received {lines_read} lines from COM4")
    else:
        print(f"  {WARN} No data received from COM4 in 5 seconds")
        print(f"       Check: device is powered on, baud rate is correct, cable is connected")
except serial.SerialException as e:
    print(f"  {FAIL} Cannot open COM4: {e}")
except Exception as e:
    print(f"  {FAIL} Error: {e}")

# ── 3. TACDev from main thread ───────────────────────────────────────────────
print("\n" + "=" * 60)
print("  3. TACDev test (main thread)")
print("=" * 60)
try:
    import TACDev
    print(f"  Alpaca version : {TACDev.AlpacaVersion()}")
    print(f"  TAC version    : {TACDev.TACVersion()}")
    count = TACDev.GetDeviceCount()
    print(f"  Device count   : {count}")
    for i in range(count):
        dev = TACDev.GetDevice(i)
        if dev:
            print(f"  Device {i}: port={dev.PortName()}  serial={dev.SerialNumber()}")
            print(f"  Trying Open() ...")
            if dev.Open():
                print(f"  {PASS} Device {i} opened successfully")
                battery = dev.GetBatteryState()
                print(f"  Battery state  : {battery}")
                dev.Close()
                print(f"  {PASS} Device {i} closed successfully")
            else:
                print(f"  {FAIL} Device {i} Open() returned False")
except ImportError:
    print(f"  {WARN} TACDev not installed")
except Exception as e:
    print(f"  {FAIL} TACDev error: {e}")

# ── 4. TACDev from a background thread ──────────────────────────────────────
print("\n" + "=" * 60)
print("  4. TACDev test (background thread – simulates asyncio executor)")
print("=" * 60)
result = [None]
def _tac_thread():
    try:
        import TACDev
        dev = TACDev.GetDevice(0)
        if dev and dev.Open():
            result[0] = f"OK: port={dev.PortName()} serial={dev.SerialNumber()}"
            dev.Close()
        else:
            result[0] = "FAIL: Open() returned False"
    except Exception as e:
        result[0] = f"ERROR: {e}"

t = threading.Thread(target=_tac_thread)
t.start()
t.join(timeout=10)
if result[0] and result[0].startswith("OK"):
    print(f"  {PASS} TACDev works from background thread: {result[0]}")
elif result[0]:
    print(f"  {FAIL} TACDev from background thread: {result[0]}")
else:
    print(f"  {FAIL} TACDev thread timed out")

print("\n" + "=" * 60)
print("  Diagnosis complete")
print("=" * 60 + "\n")