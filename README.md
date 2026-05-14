# Embedded Device Automation Framework

A framework that enables an **LLM agent running on a Linux machine** to remotely control an **embedded Linux device connected to a Windows machine** via USB — running ADB commands, fastboot commands, and streaming serial port logs back to the Linux machine over TCP.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Linux Machine                            │
│                                                                 │
│   ┌──────────────────────────────────────────────────────────┐  │
│   │  LLM Agent (LangChain / OpenAI / any framework)          │  │
│   │  Uses tools from  client/llm_tools.py                    │  │
│   └──────────────────────┬───────────────────────────────────┘  │
│                          │                                      │
│   ┌──────────────────────▼───────────────────────────────────┐  │
│   │  BridgeClient  (client/linux_client.py)                  │  │
│   │  SerialLogReceiver (client/serial_log_receiver.py)       │  │
│   └──────────────────────┬───────────────────────────────────┘  │
└──────────────────────────┼──────────────────────────────────────┘
                           │  TCP  (port 9000 – commands)
                           │  TCP  (port 9001 – serial log stream)
┌──────────────────────────┼──────────────────────────────────────┐
│                 Windows Machine                                  │
│                          │                                      │
│   ┌──────────────────────▼───────────────────────────────────┐  │
│   │  Windows Bridge Server (server/windows_bridge_server.py) │  │
│   │  ├── CommandHandler  → adb.exe / fastboot.exe / cmd.exe  │  │
│   │  └── SerialLogManager → COM port reader + broadcaster    │  │
│   └──────────────────────────────────────────────────────────┘  │
│                          │ USB                                  │
│   ┌──────────────────────▼───────────────────────────────────┐  │
│   │  Embedded Linux Device (ADB / fastboot / serial)         │  │
│   └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
t3000_/
├── server/
│   ├── windows_bridge_server.py   # Main TCP server (run on Windows)
│   ├── command_handler.py         # ADB / fastboot / shell execution
│   ├── serial_handler.py          # Serial port reader + broadcaster
│   └── config.yaml                # Server configuration
│
├── client/
│   ├── linux_client.py            # Synchronous TCP client library
│   ├── serial_log_receiver.py     # Serial log stream consumer
│   ├── llm_tools.py               # LangChain-compatible tool wrappers
│   └── config.yaml                # Client configuration
│
├── example_agent.py               # Demo: standalone + LLM agent usage
├── requirements_windows.txt       # Windows pip dependencies
├── requirements_linux.txt         # Linux pip dependencies
└── README.md
```

---

## Quick Start

### Step 1 – Set up the Windows machine

**Prerequisites:**
- Python 3.10+ installed
- `adb.exe` and `fastboot.exe` on the system PATH (from Android Platform Tools)
- The embedded device connected via USB
- The serial port identified (e.g. `COM3`)

**Install dependencies:**
```cmd
pip install -r requirements_windows.txt
```

**Edit the server config** (`server/config.yaml`):
```yaml
serial:
  port: "COM3"        # ← change to your actual COM port
  baudrate: 115200    # ← match your device's baud rate
```

**Start the bridge server:**
```cmd
python server/windows_bridge_server.py
```

You should see:
```
Windows Bridge Server started
  Command port    : 0.0.0.0:9000
  Serial log port : 0.0.0.0:9001
  Serial device   : COM3 @ 115200 baud
```

> **Firewall:** Allow inbound TCP on ports 9000 and 9001 in Windows Firewall.

---

### Step 2 – Set up the Linux machine

**Install dependencies:**
```bash
pip install -r requirements_linux.txt
```

**Edit the client config** (`client/config.yaml`):
```yaml
bridge:
  host: "192.168.1.100"   # ← IP address of the Windows machine
```

**Run the standalone connectivity demo** (no LLM required):
```bash
python example_agent.py --host 192.168.1.100 --mode demo
```

**Stream serial logs to your terminal:**
```bash
python example_agent.py --host 192.168.1.100 --mode monitor
```

**Run the LLM agent** (requires `OPENAI_API_KEY`):
```bash
export OPENAI_API_KEY="sk-..."
python example_agent.py --host 192.168.1.100 --mode agent \
  --task "Get the Android version, reboot to bootloader, list fastboot variables, then reboot normally"
```

---

## Using the Client Library Directly

```python
from client.linux_client import BridgeClient
from client.serial_log_receiver import SerialLogReceiver

# ── Command channel ──────────────────────────────────────────────
with BridgeClient(host="192.168.1.100") as client:

    # Health check
    print(client.ping())

    # List connected devices
    print(client.list_devices())

    # ADB commands
    result = client.adb("shell getprop ro.build.version.release")
    print("Android version:", result["stdout"].strip())

    result = client.adb("shell dmesg | tail -20")
    print(result["stdout"])

    # ADB file transfer
    client.adb_push("C:\\firmware\\update.zip", "/data/local/tmp/update.zip")
    client.adb_pull("/data/local/tmp/crash.log", "C:\\logs\\")

    # Fastboot commands
    result = client.adb("reboot bootloader")
    import time; time.sleep(5)
    result = client.fastboot("getvar all")
    print(result["stdout"])

    result = client.fastboot("flash boot C:\\firmware\\boot.img")
    print(result["stdout"], result["stderr"])

    client.fastboot("reboot")

    # Windows host shell
    result = client.shell("where adb")
    print("adb path:", result["stdout"].strip())

# ── Serial log stream ────────────────────────────────────────────

# Option 1: Save to file for a fixed duration
rx = SerialLogReceiver(host="192.168.1.100")
rx.save_to_file("/tmp/boot_log.txt", duration=30)

# Option 2: Iterate line by line
with SerialLogReceiver(host="192.168.1.100") as rx:
    for entry in rx:
        if entry["type"] == "serial_data":
            print(f"[{entry['timestamp']}] {entry['data']}")

# Option 3: Background thread with callback
def on_log_line(entry):
    if "ERROR" in entry.get("data", ""):
        print("⚠ Error detected:", entry["data"])

rx = SerialLogReceiver(host="192.168.1.100")
rx.start(callback=on_log_line)
time.sleep(60)
rx.stop()
```

---

## Using with a LangChain Agent

```python
import os
from langchain.agents import AgentExecutor, create_openai_functions_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI

from client.linux_client import BridgeClient
from client.llm_tools import make_tools

# Connect to bridge
client = BridgeClient(host="192.168.1.100")
client.connect()

# Create tools
tools = make_tools(client, windows_host="192.168.1.100")

# Build agent
llm = ChatOpenAI(model="gpt-4o", temperature=0)
prompt = ChatPromptTemplate.from_messages([
    ("system", "You are an embedded systems automation agent. Use the provided tools to control the device."),
    ("human", "{input}"),
    MessagesPlaceholder(variable_name="agent_scratchpad"),
])
agent = create_openai_functions_agent(llm, tools, prompt)
executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

# Run a task
result = executor.invoke({
    "input": "Check what devices are connected, get the kernel version, "
             "then capture 20 seconds of serial logs to /tmp/kernel_log.txt"
})
print(result["output"])

client.disconnect()
```

### Available LangChain Tools

| Tool name            | Description |
|----------------------|-------------|
| `ping_bridge`        | Health-check the bridge server |
| `list_devices`       | List ADB + fastboot devices |
| `adb`                | Run any ADB command |
| `adb_push`           | Push a file to the device |
| `adb_pull`           | Pull a file from the device |
| `fastboot`           | Run any fastboot command |
| `windows_shell`      | Run a command on the Windows host |
| `capture_serial_log` | Capture serial logs to a file on Linux |

---

## Wire Protocol

All messages are **newline-terminated JSON** objects sent over plain TCP.

### Command port (9000) – Request/Response

**Request** (Linux → Windows):
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "type": "adb",
  "auth": "optional-secret-token",
  "payload": {
    "command": "shell ls /",
    "serial": "",
    "timeout": 30
  }
}
```

**Response** (Windows → Linux):
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "ok",
  "returncode": 0,
  "stdout": "bin\ndev\netc\n...",
  "stderr": ""
}
```

### Serial log port (9001) – Streaming

The server continuously pushes newline-terminated JSON objects:
```json
{"type": "serial_data",   "timestamp": "2024-01-15T10:30:00.123Z", "data": "[    0.000000] Booting Linux..."}
{"type": "serial_error",  "timestamp": "2024-01-15T10:30:01.000Z", "message": "Serial port disconnected"}
{"type": "serial_status", "timestamp": "2024-01-15T10:30:01.500Z", "message": "Opened COM3 @ 115200 baud"}
{"type": "keepalive",     "message": "ping"}
```

### Supported request types

| Type          | Payload fields | Description |
|---------------|----------------|-------------|
| `ping`        | _(none)_       | Health check |
| `list_devices`| _(none)_       | List ADB + fastboot devices |
| `adb`         | `command`, `serial`, `timeout` | Run ADB command |
| `adb_push`    | `local`, `remote`, `serial`, `timeout` | Push file to device |
| `adb_pull`    | `remote`, `local`, `serial`, `timeout` | Pull file from device |
| `fastboot`    | `command`, `serial`, `timeout` | Run fastboot command |
| `shell`       | `command`, `timeout` | Run Windows host shell command |

---

## Authentication

To require a secret token, set `auth_token` in both configs:

**`server/config.yaml`:**
```yaml
server:
  auth_token: "my-secret-token-123"
```

**`client/config.yaml`:**
```yaml
bridge:
  auth_token: "my-secret-token-123"
```

Or pass it programmatically:
```python
client = BridgeClient(host="192.168.1.100", auth_token="my-secret-token-123")
```

---

## Configuration Reference

### Server (`server/config.yaml`)

| Key | Default | Description |
|-----|---------|-------------|
| `server.host` | `0.0.0.0` | Listen address |
| `server.command_port` | `9000` | Command TCP port |
| `server.serial_log_port` | `9001` | Serial log TCP port |
| `server.auth_token` | `""` | Auth token (empty = no auth) |
| `server.max_clients` | `5` | Max simultaneous command clients |
| `adb.path` | `adb` | Path to adb.exe |
| `adb.default_timeout` | `30` | ADB command timeout (seconds) |
| `fastboot.path` | `fastboot` | Path to fastboot.exe |
| `fastboot.default_timeout` | `120` | Fastboot command timeout (seconds) |
| `serial.port` | `COM3` | Windows COM port |
| `serial.baudrate` | `115200` | Serial baud rate |
| `serial.log_file` | `serial_output.log` | Local serial log file on Windows |

### Client (`client/config.yaml`)

| Key | Default | Description |
|-----|---------|-------------|
| `bridge.host` | `192.168.1.100` | Windows machine IP |
| `bridge.command_port` | `9000` | Command port |
| `bridge.serial_log_port` | `9001` | Serial log port |
| `bridge.auth_token` | `""` | Auth token |
| `bridge.connect_timeout` | `10` | Connection timeout (seconds) |
| `bridge.recv_timeout` | `60` | Response timeout (seconds) |
| `bridge.auto_reconnect` | `true` | Auto-reconnect on connection loss |

---

## Troubleshooting

**`adb: command not found` on Windows**
- Install [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools) and add to PATH.

**`pyserial not installed` in server logs**
- Run `pip install pyserial` on the Windows machine.

**Serial port not opening (`Access is denied` or `COM3 not found`)**
- Check Device Manager for the correct COM port number.
- Make sure no other application (e.g. PuTTY, Arduino IDE) has the port open.
- Try running the server as Administrator.

**Connection refused from Linux**
- Verify the Windows Firewall allows inbound TCP on ports 9000 and 9001.
- Check the Windows machine IP address with `ipconfig`.

**LangChain agent not finding tools**
- Ensure `langchain>=0.2.0` is installed: `pip install -r requirements_linux.txt`
- Verify `OPENAI_API_KEY` is set in the environment.

**Serial logs not appearing**
- The serial reader retries automatically every 3 seconds on error.
- Check `bridge_server.log` on the Windows machine for serial port errors.
- Verify the device is powered on and outputting data at the configured baud rate.