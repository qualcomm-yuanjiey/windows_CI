# Linux Machine Setup Guide
## Embedded Device Automation Framework – Client Side

This folder contains everything the LLM agent needs on the **Linux machine**.
The Windows machine runs the bridge server (`server/`) separately.

---

## What's in this folder

```
linux_deploy/
├── linux_client.py          # BridgeClient – sends commands to the Windows bridge
├── serial_log_receiver.py   # SerialLogReceiver – streams serial logs from Windows
├── llm_tools.py             # 23 LangChain-compatible tools for the LLM agent
├── config.yaml              # ← EDIT THIS: set bridge.host to Windows machine IP
├── requirements_linux.txt   # pip dependencies
├── example_agent.py         # Example LLM agent usage
└── README_LINUX.md          # This file
```

---

## Step 1 – Copy this folder to the Linux machine

```bash
# From the Windows machine (or via SCP from Linux):
scp -r linux_deploy/  user@linux-machine:~/bridge_client/
```

---

## Step 2 – Edit config.yaml

```bash
nano ~/bridge_client/config.yaml
```

Change:
```yaml
bridge:
  host: "WINDOWS_MACHINE_IP"   # ← replace with actual IP, e.g. "192.168.1.100"
```

Find the Windows machine IP with `ipconfig` on the Windows machine.

---

## Step 3 – Install dependencies

```bash
cd ~/bridge_client
pip install -r requirements_linux.txt
```

---

## Step 4 – Make sure the Windows bridge server is running

On the **Windows machine**:
```cmd
python server/windows_bridge_server.py
```

You should see:
```
Windows Bridge Server started
  Command port      : 0.0.0.0:9000
  Serial log port   : 0.0.0.0:9001
  Serial UART port  : COM4 @ 115200 baud
  TACDev VTP number : VTP1
```

---

## Step 5 – Test connectivity

```bash
cd ~/bridge_client
python -c "
from linux_client import BridgeClient
with BridgeClient(host='192.168.1.100') as c:
    print(c.ping())
    print(c.tac_list_devices())
"
```

---

## Step 6 – Run the LLM agent

```bash
export OPENAI_API_KEY="sk-..."
python example_agent.py --host 192.168.1.100 --mode agent \
  --task "Power cycle the device, boot to fastboot, then boot the kernel from \\\\buildserver\\share\\latest\\boot.img and capture serial logs until init"
```

---

## Available BridgeClient methods

### General
| Method | Description |
|--------|-------------|
| `ping()` | Health-check the bridge server |
| `list_devices()` | List ADB + fastboot devices |

### ADB
| Method | Description |
|--------|-------------|
| `adb(command)` | Run any ADB command |
| `adb_push(local, remote)` | Push file to device |
| `adb_pull(remote, local)` | Pull file from device |

### Fastboot
| Method | Description |
|--------|-------------|
| `fastboot(command)` | Run any fastboot command |
| `fastboot_boot(image_path)` | Boot kernel image without flashing (UNC path supported) |

### Windows host shell
| Method | Description |
|--------|-------------|
| `shell(command)` | Run a command on the Windows host |

### TACDev power control
| Method | Description |
|--------|-------------|
| `tac_list_devices()` | List TAC devices |
| `tac_get_info(vtp_number="VTP1")` | Get device info |
| `tac_get_status(vtp_number="VTP1")` | Get USB/charging/temp/voltage/current |
| `tac_power_off(vtp_number="VTP1")` | Power OFF [5 s sleep after] |
| `tac_power_on(vtp_number="VTP1")` | Power ON [5 s sleep after] |
| `tac_power_cycle(vtp_number="VTP1")` | Power cycle [5 s sleep after each step] |
| `tac_boot_edl(vtp_number="VTP1")` | Boot to EDL mode [5 s sleep after] |
| `tac_boot_fastboot(vtp_number="VTP1")` | Boot to Fastboot mode [5 s sleep after] |
| `tac_boot_uefi(vtp_number="VTP1")` | Boot to UEFI menu [5 s sleep after] |
| `tac_boot_recovery(vtp_number="VTP1")` | Boot to Recovery mode [5 s sleep after] |
| `tac_boot_normal(vtp_number="VTP1")` | Boot to Normal mode [5 s sleep after] |
| `tac_set_usb(state, vtp_number="VTP1")` | Enable/disable USB |

### Serial logs
```python
from serial_log_receiver import SerialLogReceiver

# Save 60 seconds of serial logs to a file
rx = SerialLogReceiver(host="192.168.1.100")
rx.save_to_file("/tmp/serial_logs/boot.log", duration=60)

# Stream live
with SerialLogReceiver(host="192.168.1.100") as rx:
    for entry in rx:
        if entry["type"] == "serial_data":
            print(entry["data"])
```

---

## LangChain tools (for LLM agents)

```python
from linux_client import BridgeClient
from llm_tools import make_tools

client = BridgeClient(host="192.168.1.100")
client.connect()
tools = make_tools(client, windows_host="192.168.1.100")
# tools contains 23 BaseTool objects ready for any LangChain agent
```

### Tool names
`ping_bridge`, `list_devices`, `adb`, `adb_push`, `adb_pull`,
`fastboot`, `fastboot_boot`, `windows_shell`, `capture_serial_log`,
`tac_list_devices`, `tac_get_versions`, `tac_get_info`, `tac_get_status`,
`tac_power_off`, `tac_power_on`, `tac_power_cycle`,
`tac_boot_edl`, `tac_boot_fastboot`, `tac_boot_uefi`,
`tac_boot_secondary_edl`, `tac_boot_recovery`, `tac_boot_normal`,
`tac_set_usb`

---

## fastboot_boot – network path example

The `fastboot_boot` tool lets the LLM agent specify a network path to a build server:

```python
# Boot a kernel from a UNC network share (no flashing)
result = client.fastboot_boot(r"\\buildserver\share\builds\latest\boot.img")

# Or from a local Windows staging directory
result = client.fastboot_boot(r"C:\firmware\staging\boot.img")
```

The LLM agent decides the path based on context (build number, branch, etc.).