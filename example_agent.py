#!/usr/bin/env python3
"""
example_agent.py
================
Demonstrates how to use the automation framework with a LangChain LLM agent
running on a Linux machine to control an embedded Linux device connected to
a remote Windows machine.

Prerequisites (Linux machine)
------------------------------
1. Install dependencies:
       pip install -r requirements_linux.txt

2. Set your OpenAI API key:
       export OPENAI_API_KEY="sk-..."

3. Edit WINDOWS_HOST below (or set the BRIDGE_HOST env variable) to point
   to the IP address of the Windows machine running windows_bridge_server.py.

4. Make sure the Windows bridge server is running:
       python server/windows_bridge_server.py

Running
-------
    python example_agent.py

    # Or with a custom task:
    python example_agent.py --task "Flash the boot partition with /firmware/boot.img"
"""

import argparse
import logging
import os
import sys

# ---------------------------------------------------------------------------
# Configuration – edit these or set environment variables
# ---------------------------------------------------------------------------
WINDOWS_HOST = os.environ.get("BRIDGE_HOST", "192.168.1.100")
COMMAND_PORT = int(os.environ.get("BRIDGE_CMD_PORT", "9000"))
SERIAL_LOG_PORT = int(os.environ.get("BRIDGE_SERIAL_PORT", "9001"))
AUTH_TOKEN = os.environ.get("BRIDGE_AUTH_TOKEN", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("example_agent")


# ---------------------------------------------------------------------------
# Standalone demo (no LangChain required)
# ---------------------------------------------------------------------------

def run_standalone_demo(host: str):
    """
    Demonstrate the framework without an LLM – useful for testing connectivity
    and verifying the bridge server is working correctly.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "client"))
    from linux_client import BridgeClient, BridgeClientError
    from serial_log_receiver import SerialLogReceiver

    print("\n" + "=" * 60)
    print("  Standalone Bridge Client Demo")
    print("=" * 60)

    # ---- Command channel ---------------------------------------------------
    print(f"\n[1] Connecting to bridge server at {host}:{COMMAND_PORT} …")
    try:
        with BridgeClient(
            host=host,
            command_port=COMMAND_PORT,
            auth_token=AUTH_TOKEN,
        ) as client:

            # Ping
            print("\n[2] Ping …")
            result = client.ping()
            print(f"    {result}")

            # List devices
            print("\n[3] Listing ADB + fastboot devices …")
            result = client.list_devices()
            print(f"    ADB devices:\n{result.get('adb_devices', '').strip()}")
            print(f"    Fastboot devices:\n{result.get('fastboot_devices', '').strip()}")

            # ADB shell command
            print("\n[4] Running: adb shell getprop ro.build.version.release …")
            result = client.adb("shell getprop ro.build.version.release")
            if result["status"] == "ok":
                print(f"    Android version: {result['stdout'].strip()}")
            else:
                print(f"    Error: {result['stderr']}")

            # ADB shell – list /proc
            print("\n[5] Running: adb shell ls /proc …")
            result = client.adb("shell ls /proc", timeout=10)
            if result["status"] == "ok":
                lines = result["stdout"].strip().splitlines()
                print(f"    /proc entries (first 10): {lines[:10]}")
            else:
                print(f"    Error: {result['stderr']}")

            # Fastboot devices
            print("\n[6] Running: fastboot devices …")
            result = client.fastboot("devices", timeout=10)
            print(f"    {result['stdout'].strip() or '(no fastboot devices)'}")

            # Windows host shell
            print("\n[7] Running Windows host command: where adb …")
            result = client.shell("where adb")
            print(f"    adb path: {result['stdout'].strip()}")

    except BridgeClientError as exc:
        print(f"\n[ERROR] Bridge client error: {exc}")
        print("  Make sure the Windows bridge server is running.")
        return

    # ---- Serial log stream -------------------------------------------------
    print("\n[8] Capturing 10 seconds of serial logs …")
    log_path = "/tmp/device_serial_demo.log"
    rx = SerialLogReceiver(
        host=host,
        serial_log_port=SERIAL_LOG_PORT,
        auth_token=AUTH_TOKEN,
    )
    try:
        rx.save_to_file(log_path, duration=10, include_timestamps=True)
        with open(log_path) as fh:
            lines = fh.readlines()
        print(f"    Captured {len(lines)} lines → {log_path}")
        if lines:
            print(f"    First line: {lines[0].rstrip()}")
    except Exception as exc:  # noqa: BLE001
        print(f"    Serial log capture failed: {exc}")

    print("\n" + "=" * 60)
    print("  Demo complete!")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# LangChain agent demo
# ---------------------------------------------------------------------------

def run_agent_demo(host: str, task: str):
    """
    Run a LangChain ReAct / OpenAI-functions agent with the bridge tools.
    Requires: langchain, langchain-openai, openai, OPENAI_API_KEY env var.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "client"))

    try:
        from langchain.agents import AgentExecutor, create_openai_functions_agent
        from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        print(f"[ERROR] LangChain not installed: {exc}")
        print("  Run: pip install -r requirements_linux.txt")
        sys.exit(1)

    from linux_client import BridgeClient
    from llm_tools import make_tools

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("[ERROR] OPENAI_API_KEY environment variable is not set.")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("  LangChain Agent Demo")
    print(f"  Task: {task}")
    print("=" * 60)

    # Connect to bridge
    client = BridgeClient(
        host=host,
        command_port=COMMAND_PORT,
        auth_token=AUTH_TOKEN,
    )
    client.connect()
    log.info("Connected to bridge server")

    # Build tools
    tools = make_tools(
        client=client,
        windows_host=host,
        serial_log_port=SERIAL_LOG_PORT,
        auth_token=AUTH_TOKEN,
    )

    # Build LLM
    llm = ChatOpenAI(model="gpt-4o", temperature=0, api_key=api_key)

    # System prompt
    system_prompt = (
        "You are an embedded systems automation agent. "
        "You have access to tools that let you control an embedded Linux device "
        "connected to a Windows machine via USB. "
        "You can run ADB commands, fastboot commands, Windows host shell commands, "
        "and capture serial port logs. "
        "Always check what devices are connected before running device-specific commands. "
        "Be careful with destructive operations like 'erase' or 'flash' – confirm the "
        "device serial and partition before proceeding. "
        "When capturing serial logs, save them to /tmp/serial_logs/ on this Linux machine."
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "{input}"),
        MessagesPlaceholder(variable_name="agent_scratchpad"),
    ])

    # Create agent
    agent = create_openai_functions_agent(llm, tools, prompt)
    executor = AgentExecutor(agent=agent, tools=tools, verbose=True, max_iterations=15)

    try:
        result = executor.invoke({"input": task})
        print("\n" + "=" * 60)
        print("Agent result:")
        print(result.get("output", result))
        print("=" * 60 + "\n")
    finally:
        client.disconnect()



# ---------------------------------------------------------------------------
# TACDev debug (power on / off / cycle / status / list)
# ---------------------------------------------------------------------------

def run_tac_debug(host: str, action: str, shell_script: str = ""):
    """
    Debug branch for TACDev power control.

    Supported actions
    -----------------
    on      – power on  the board  (PowerOnButton)
    off     – power off the board  (PowerOffButton)
    cycle   – power cycle          (off -> sleep -> on)
    status  – print device info + runtime status
    list    – list all TAC devices visible to the Windows machine
    edl      – boot to EDL (Emergency Download) mode
    fastboot – boot to Fastboot mode
    normal   – boot to Normal (Android) mode
    recovery – boot to Recovery mode
    shell    – run a shell script / command on the Windows host (use with --shell-script)
    execute_action – run a Windows cmd command on the host, e.g.
                     --tac-action execute_action "dir C:\\"
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "client"))
    from linux_client import BridgeClient, BridgeClientError

    valid_actions = ("on", "off", "cycle", "status", "list",
                     "edl", "fastboot", "normal", "recovery", "shell",
                     "execute_action")
    if action not in valid_actions:
        print(f"[ERROR] Unknown tac-action \'{action}\'. Choose from: {', '.join(valid_actions)}")
        sys.exit(1)

    print("\n" + "=" * 60)
    print(f"  TACDev Debug – action: {action}")
    print("=" * 60)

    try:
        # For the shell action the script may run up to 300 s; give the
        # socket an extra 60 s buffer so it does not time out mid-run.
        recv_timeout = 360 if action in ("shell", "execute_action") else 120
        with BridgeClient(host=host, command_port=COMMAND_PORT,
                          auth_token=AUTH_TOKEN,
                          recv_timeout=recv_timeout) as client:

            if action == "list":
                print("\n[TAC] Listing all TAC devices …")
                result = client.tac_list_devices()
                print(result.get("stdout", ""))

            elif action == "status":
                print("\n[TAC] Getting device info …")
                info = client.tac_get_info()
                print(info.get("stdout", ""))
                print("\n[TAC] Getting runtime status …")
                status = client.tac_get_status()
                print(status.get("stdout", ""))

            elif action == "on":
                print("\n[TAC] Powering ON the board …")
                result = client.tac_power_on()
                print(result.get("stdout", ""))
                print(f"status: {result['status']}")

            elif action == "off":
                print("\n[TAC] Powering OFF the board …")
                result = client.tac_power_off()
                print(result.get("stdout", ""))
                print(f"status: {result['status']}")

            elif action == "cycle":
                print("\n[TAC] Power cycling the board (off -> sleep -> on) …")
                result = client.tac_power_cycle()
                print(result.get("stdout", ""))
                print(f"status: {result['status']}")

            elif action == "edl":
                print("\n[TAC] Booting to EDL mode …")
                result = client.tac_boot_edl()
                print(result.get("stdout", ""))
                print(f"status: {result['status']}")

            elif action == "fastboot":
                print("\n[TAC] Booting to Fastboot mode …")
                result = client.tac_boot_fastboot()
                print(result.get("stdout", ""))
                print(f"status: {result['status']}")

            elif action == "normal":
                print("\n[TAC] Booting to Normal mode …")
                result = client.tac_boot_normal()
                print(result.get("stdout", ""))
                print(f"status: {result['status']}")

            elif action == "recovery":
                print("\n[TAC] Booting to Recovery mode …")
                result = client.tac_boot_recovery()
                print(result.get("stdout", ""))
                print(f"status: {result['status']}")

            elif action in ("shell", "execute_action"):
                if not shell_script:
                    if action == "shell":
                        print("[ERROR] --shell-script is required for action 'shell'")
                    else:
                        print("[ERROR] a command is required, e.g. "
                              "--tac-action execute_action \"dir C:\\\"")
                    sys.exit(1)
                print(f"\n[SHELL] Running on Windows host: {shell_script} …")
                result = client.shell(shell_script, timeout=300)
                stdout = result.get("stdout", "").strip()
                stderr = result.get("stderr", "").strip()
                if stdout:
                    print(stdout)
                if stderr:
                    print(f"[stderr]\n{stderr}")
                print(f"status: {result['status']}  returncode: {result.get('returncode', '?')}")

    except BridgeClientError as exc:
        print(f"\n[ERROR] {exc}")
        print("  Make sure the Windows bridge server is running.")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("  TACDev debug done.")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Serial log monitor (standalone, no LLM)
# ---------------------------------------------------------------------------

def run_serial_monitor(host: str):
    """
    Continuously print serial log lines to stdout.
    Press Ctrl+C to stop.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "client"))
    from serial_log_receiver import SerialLogReceiver

    print(f"\nMonitoring serial logs from {host}:{SERIAL_LOG_PORT} …  (Ctrl+C to stop)\n")
    rx = SerialLogReceiver(
        host=host,
        serial_log_port=SERIAL_LOG_PORT,
        auth_token=AUTH_TOKEN,
        recv_timeout=None,  # block forever
    )
    with rx:
        try:
            for entry in rx:
                entry_type = entry.get("type", "")
                ts = entry.get("timestamp", "")
                if entry_type == "serial_data":
                    print(f"[{ts}] {entry.get('data', '')}")
                elif entry_type in ("serial_error", "serial_status"):
                    print(f"[{ts}] *** {entry_type.upper()}: {entry.get('message', '')} ***")
        except KeyboardInterrupt:
            print("\nMonitor stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cycle + serial log capture
# ---------------------------------------------------------------------------

def run_cycle_and_log(host: str, duration: float, output: str):
    """
    1. Subscribe to the serial log stream BEFORE power cycle so no boot
       output is missed.
    2. Power cycle the board via TACDev.
    3. Capture serial logs for ``duration`` seconds (or until Ctrl+C).
    4. Save the log to ``output`` on this machine.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "client"))
    from linux_client import BridgeClient, BridgeClientError
    from serial_log_receiver import SerialLogReceiver
    import threading

    print("\n" + "=" * 60)
    print("  Cycle + Serial Log Capture")
    print(f"  host     : {host}")
    print(f"  duration : {duration}s")
    print(f"  output   : {output}")
    print("=" * 60)

    # ── Step 1: start serial capture in background BEFORE power cycle ──
    lines = []
    stop_event = threading.Event()

    def _capture():
        rx = SerialLogReceiver(
            host=host,
            serial_log_port=SERIAL_LOG_PORT,
            auth_token=AUTH_TOKEN,
            recv_timeout=duration,
        )
        rx.connect()
        log.info("Serial capture connected – waiting for boot output …")
        try:
            for entry in rx:
                if stop_event.is_set():
                    break
                entry_type = entry.get("type", "")
                ts         = entry.get("timestamp", "")
                if entry_type == "serial_data":
                    line = entry.get("data", "")
                    lines.append(f"[{ts}] {line}")
                    print(f"  {line}")
                elif entry_type in ("serial_error", "serial_status"):
                    msg = entry.get("message", "")
                    lines.append(f"[{ts}] *** {entry_type.upper()}: {msg} ***")
                    print(f"  *** {entry_type.upper()}: {msg} ***")
        finally:
            rx.disconnect()

    capture_thread = threading.Thread(target=_capture, daemon=True, name="serial-capture")
    capture_thread.start()
    import time as _time
    _time.sleep(1.0)   # let capture thread connect before power cycle

    # ── Step 2: power cycle ────────────────────────────────────────────
    print("\n[TAC] Power cycling the board …")
    try:
        with BridgeClient(host=host, command_port=COMMAND_PORT, auth_token=AUTH_TOKEN) as client:
            result = client.tac_power_cycle()
            if result.get("status") != "ok":
                print(f"[ERROR] Power cycle failed: {result.get('stderr', '')}")
                stop_event.set()
                return
            print(f"[TAC] {result.get('stdout', '').strip()}")
    except BridgeClientError as exc:
        print(f"[ERROR] {exc}")
        stop_event.set()
        return

    # ── Step 3: wait for capture duration ─────────────────────────────
    print(f"\n[LOG] Capturing for {duration}s … (Ctrl+C to stop early)\n")
    try:
        capture_thread.join(timeout=duration)
    except KeyboardInterrupt:
        print("\n[LOG] Interrupted by user.")
    finally:
        stop_event.set()
        capture_thread.join(timeout=5)

    # ── Step 4: save log ───────────────────────────────────────────────
    import os as _os
    _os.makedirs(_os.path.dirname(_os.path.abspath(output)), exist_ok=True)
    with open(output, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print("\n" + "=" * 60)
    print(f"  Lines captured : {len(lines)}")
    print(f"  Log saved to   : {output}")
    print("=" * 60 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Example usage of the Windows Bridge automation framework"
    )
    parser.add_argument(
        "--host",
        default=WINDOWS_HOST,
        help=f"IP address of the Windows bridge server (default: {WINDOWS_HOST})",
    )
    parser.add_argument(
        "--mode",
        choices=["demo", "agent", "monitor", "debug", "cycle-log"],
        default="demo",
        help=(
            "demo    – standalone connectivity demo (no LLM required)\n"
            "agent   – run a LangChain LLM agent (requires OPENAI_API_KEY)\n"
            "monitor – stream serial logs to stdout\n"
            "debug     – TACDev power control (use with --tac-action)\n"
            "cycle-log – power cycle then capture serial log (use with --log-duration / --log-output)"
        ),
    )
    parser.add_argument(
        "--task",
        default=(
            "List all connected ADB and fastboot devices, get the Android version "
            "from the device, then capture 15 seconds of serial logs and save them "
            "to /tmp/serial_logs/boot_log.txt"
        ),
        help="Task description for the LLM agent (used with --mode agent)",
    )
    parser.add_argument(
        "--tac-action",
        choices=["on", "off", "cycle", "status", "list",
                 "edl", "fastboot", "normal", "recovery", "shell",
                 "execute_action"],
        default="cycle",
        help=(
            "TACDev action for --mode debug:\n"
            "  on     – power on the board\n"
            "  off    – power off the board\n"
            "  cycle  – power cycle (off -> sleep -> on)\n"
            "  status – show device info and runtime status\n"
            "  list   – list all TAC devices\n"
            "  execute_action \"CMD\" – run a Windows cmd command on the host"
        ),
    )
    parser.add_argument(
        "action_command",
        nargs="*",
        default=[],
        help=(
            "Windows cmd command for --tac-action execute_action. "
            "Example: --tac-action execute_action \"ipconfig /all\""
        ),
    )
    parser.add_argument(
        "--shell-script",
        default="",
        help=(
            "Shell script or command to run on the Windows host "
            "(used with --mode debug --tac-action shell). "
            "Example: --shell-script flash.bat"
        ),
    )
    parser.add_argument(
        "--log-duration",
        type=float,
        default=60.0,
        help="Seconds to capture serial log after power cycle (default: 60, used with --mode cycle-log)",
    )
    parser.add_argument(
        "--log-output",
        default="/tmp/serial_logs/boot_log.txt",
        help="File path to save the serial log (default: /tmp/serial_logs/boot_log.txt, used with --mode cycle-log)",
    )
    args = parser.parse_args()

    if args.mode == "demo":
        run_standalone_demo(args.host)
    elif args.mode == "agent":
        run_agent_demo(args.host, args.task)
    elif args.mode == "monitor":
        run_serial_monitor(args.host)
    elif args.mode == "debug":
        shell_script = args.shell_script
        if args.tac_action == "execute_action" and args.action_command:
            shell_script = " ".join(args.action_command)
        run_tac_debug(args.host, args.tac_action, shell_script)
    elif args.mode == "cycle-log":
        run_cycle_and_log(args.host, args.log_duration, args.log_output)
