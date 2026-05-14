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
        choices=["demo", "agent", "monitor"],
        default="demo",
        help=(
            "demo    – standalone connectivity demo (no LLM required)\n"
            "agent   – run a LangChain LLM agent (requires OPENAI_API_KEY)\n"
            "monitor – stream serial logs to stdout"
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
    args = parser.parse_args()

    if args.mode == "demo":
        run_standalone_demo(args.host)
    elif args.mode == "agent":
        run_agent_demo(args.host, args.task)
    elif args.mode == "monitor":
        run_serial_monitor(args.host)