"""
llm_tools.py
============
LangChain-compatible tool wrappers around BridgeClient and SerialLogReceiver.

These tools let an LLM agent control an embedded Linux device connected to a
remote Windows machine via the Windows Bridge Server.

Available tools
---------------
  General:
    ping_bridge           - check bridge server connectivity
    list_devices          - list ADB / fastboot devices

  ADB:
    adb                   - run any ADB command
    adb_push              - push a file to the device
    adb_pull              - pull a file from the device

  Fastboot:
    fastboot              - run any fastboot command

  Windows host:
    windows_shell         - run a command on the Windows host

  Serial logs:
    capture_serial_log    - capture serial logs to a file on the Linux machine

  TACDev (Qualcomm TAC power/boot control):
    tac_list_devices      - list all TAC devices (index, VTP, port, serial)
    tac_get_versions      - get Alpaca / TAC library versions
    tac_get_info          - get device info (VTP, port, serial, battery)
    tac_get_status        - get USB state, charging, temperature, voltage, current
    tac_power_off         - power off the device              [5 s sleep after]
    tac_power_on          - power on the device               [5 s sleep after]
    tac_power_cycle       - power cycle (off->sleep->on->sleep)
    tac_boot_edl          - boot to EDL mode                  [5 s sleep after]
    tac_boot_fastboot     - boot to Fastboot mode             [5 s sleep after]
    tac_boot_uefi         - boot to UEFI menu                 [5 s sleep after]
    tac_boot_secondary_edl- boot to secondary EDL mode        [5 s sleep after]
    tac_boot_recovery     - boot to Recovery mode             [5 s sleep after]
    tac_boot_normal       - boot to Normal (Android) mode     [5 s sleep after]
    tac_set_usb           - enable or disable USB connection

Usage
-----
    from linux_client import BridgeClient
    from llm_tools import make_tools

    client = BridgeClient(host="192.168.1.100")
    client.connect()
    tools = make_tools(client, windows_host="192.168.1.100")

    from langchain.agents import AgentExecutor, create_openai_functions_agent
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    llm = ChatOpenAI(model="gpt-4o", temperature=0)
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are an embedded systems automation agent."),
        ("human", "{input}"),
        MessagesPlaceholder(variable_name="agent_scratchpad"),
    ])
    agent = create_openai_functions_agent(llm, tools, prompt)
    executor = AgentExecutor(agent=agent, tools=tools, verbose=True)
    executor.invoke({"input": "Power cycle the device and capture 30s of serial logs"})
"""

import logging
from typing import List, Optional, Type

log = logging.getLogger("bridge.llm_tools")

# ---------------------------------------------------------------------------
# LangChain / pydantic import guard
# ---------------------------------------------------------------------------
try:
    from langchain.tools import BaseTool
    from pydantic import BaseModel, Field
    _LANGCHAIN_AVAILABLE = True
except ImportError:
    _LANGCHAIN_AVAILABLE = False

    class BaseTool:  # type: ignore[no-redef]
        pass

    class BaseModel:  # type: ignore[no-redef]
        pass

    def Field(*args, **kwargs):  # type: ignore[no-redef]
        return None


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------

class _EmptyInput(BaseModel):
    pass


class _AdbInput(BaseModel):
    command: str = Field(description="ADB sub-command, e.g. 'shell ls /' or 'reboot bootloader'")
    serial: str = Field(default="", description="Override the configured ADB device serial (optional)")
    timeout: int = Field(default=30, description="Command timeout in seconds")


class _AdbPushInput(BaseModel):
    local: str = Field(description="Absolute Windows path to the file to push, e.g. C:\\firmware\\boot.img")
    remote: str = Field(description="Destination path on the device, e.g. /data/local/tmp/boot.img")
    serial: str = Field(default="", description="Override the configured ADB device serial (optional)")
    timeout: int = Field(default=60, description="Transfer timeout in seconds")


class _AdbPullInput(BaseModel):
    remote: str = Field(description="Path on the device to pull, e.g. /data/local/tmp/log.txt")
    local: str = Field(default=".", description="Destination path on the Windows machine")
    serial: str = Field(default="", description="Override the configured ADB device serial (optional)")
    timeout: int = Field(default=60, description="Transfer timeout in seconds")


class _FastbootInput(BaseModel):
    command: str = Field(
        description="Fastboot sub-command, e.g. 'devices', 'flash boot boot.img', 'reboot', 'getvar all'"
    )
    serial: str = Field(default="", description="Override the configured fastboot device serial (optional)")
    timeout: int = Field(default=120, description="Command timeout in seconds")


class _FastbootBootInput(BaseModel):
    image_path: str = Field(
        description=(
            "Windows-accessible path to the boot image to load via 'fastboot boot'. "
            "Can be a local Windows path (e.g. C:\\\\images\\\\boot.img) or a "
            "UNC network path (e.g. \\\\\\\\fileserver\\\\share\\\\builds\\\\boot.img). "
            "The device must already be in fastboot/bootloader mode."
        )
    )
    serial: str = Field(default="", description="Override the configured fastboot device serial (optional)")
    timeout: int = Field(default=120, description="Command timeout in seconds")


class _ShellInput(BaseModel):
    command: str = Field(description="Windows host shell command via cmd.exe, e.g. 'dir C:\\firmware'")
    timeout: int = Field(default=60, description="Command timeout in seconds")


class _CaptureSerialLogInput(BaseModel):
    output_path: str = Field(description="Absolute path on the Linux machine where the log file will be saved")
    duration: float = Field(default=30.0, description="How many seconds to capture serial logs")
    include_timestamps: bool = Field(default=True, description="Prepend each line with an ISO-8601 timestamp")
    raw_only: bool = Field(default=False, description="Write only raw data lines (no timestamps, no status messages)")


class _TacInput(BaseModel):
    vtp_number: str = Field(
        default="",
        description=(
            "VTP number of the TAC device to target (e.g. 'VTP12345'). "
            "Leave empty to use the server-configured VTP/device. "
            "Use tac_list_devices to discover available VTP numbers."
        ),
    )
    device_number: int = Field(
        default=-1,
        description="TAC device index (0-based). Use -1 to use server default. Ignored if vtp_number is set.",
    )
    sleep: float = Field(
        default=-1.0,
        description="Seconds to sleep after the power/boot action. Use -1 for server default (5 s).",
    )


class _TacSetUsbInput(BaseModel):
    state: bool = Field(description="True to enable USB, False to disable USB")
    vtp_number: str = Field(default="", description="VTP number of the TAC device (optional)")
    device_number: int = Field(default=-1, description="TAC device index. Use -1 for server default.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(result: dict) -> str:
    """Format a bridge server result dict into a readable string for the LLM."""
    parts = [f"Status: {result.get('status', 'unknown')}"]
    rc = result.get("returncode", "")
    if rc != "":
        parts.append(f"Return code: {rc}")
    stdout = result.get("stdout", "").strip()
    stderr = result.get("stderr", "").strip()
    if stdout:
        parts.append(f"Output:\n{stdout}")
    if stderr:
        parts.append(f"Stderr:\n{stderr}")
    for key in ("message", "adb_devices", "fastboot_devices", "timestamp",
                "alpaca_version", "tac_version", "battery_state",
                "vtp_number", "port_name", "serial_number", "device_count",
                "usb_state", "charging_state", "temperature", "voltage", "current"):
        val = result.get(key)
        if val is not None and val != "":
            parts.append(f"{key.replace('_', ' ').title()}: {val}")
    return "\n".join(parts)


def _tac_payload(vtp_number: str, device_number: int, sleep: float, **extra) -> dict:
    """Build a TACDev payload, omitting fields that should use server defaults."""
    p = dict(extra)
    if vtp_number:
        p["vtp_number"] = vtp_number
    if device_number >= 0:
        p["device_number"] = device_number
    if sleep >= 0:
        p["sleep"] = sleep
    return p


# ---------------------------------------------------------------------------
# General tools
# ---------------------------------------------------------------------------

class PingTool(BaseTool):
    name: str = "ping_bridge"
    description: str = "Ping the Windows bridge server to verify connectivity. Returns pong with a timestamp."
    args_schema: Type[BaseModel] = _EmptyInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, **kwargs) -> str:
        return _fmt(self._client.ping())

    async def _arun(self, **kwargs) -> str:
        return self._run(**kwargs)


class ListDevicesTool(BaseTool):
    name: str = "list_devices"
    description: str = (
        "List all ADB and fastboot devices connected to the Windows machine. "
        "Also shows the configured default device serials."
    )
    args_schema: Type[BaseModel] = _EmptyInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, **kwargs) -> str:
        return _fmt(self._client.list_devices())

    async def _arun(self, **kwargs) -> str:
        return self._run(**kwargs)


# ---------------------------------------------------------------------------
# ADB tools
# ---------------------------------------------------------------------------

class AdbTool(BaseTool):
    name: str = "adb"
    description: str = (
        "Run an ADB command on the embedded device. "
        "Examples: 'shell getprop ro.build.version.release', 'shell dmesg', "
        "'reboot bootloader', 'logcat -d'. "
        "The server uses the configured ADB serial by default; pass 'serial' to override."
    )
    args_schema: Type[BaseModel] = _AdbInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, command: str, serial: str = "", timeout: int = 30) -> str:
        return _fmt(self._client.adb(command, serial=serial, timeout=timeout))

    async def _arun(self, command: str, serial: str = "", timeout: int = 30) -> str:
        return self._run(command, serial=serial, timeout=timeout)


class AdbPushTool(BaseTool):
    name: str = "adb_push"
    description: str = (
        "Push a file from the Windows machine to the embedded device. "
        "'local' is an absolute Windows path; 'remote' is the device destination path."
    )
    args_schema: Type[BaseModel] = _AdbPushInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, local: str, remote: str, serial: str = "", timeout: int = 60) -> str:
        return _fmt(self._client.adb_push(local, remote, serial=serial, timeout=timeout))

    async def _arun(self, local: str, remote: str, serial: str = "", timeout: int = 60) -> str:
        return self._run(local, remote, serial=serial, timeout=timeout)


class AdbPullTool(BaseTool):
    name: str = "adb_pull"
    description: str = (
        "Pull a file from the embedded device to the Windows machine. "
        "'remote' is the device path; 'local' is the Windows destination."
    )
    args_schema: Type[BaseModel] = _AdbPullInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, remote: str, local: str = ".", serial: str = "", timeout: int = 60) -> str:
        return _fmt(self._client.adb_pull(remote, local=local, serial=serial, timeout=timeout))

    async def _arun(self, remote: str, local: str = ".", serial: str = "", timeout: int = 60) -> str:
        return self._run(remote, local=local, serial=serial, timeout=timeout)


# ---------------------------------------------------------------------------
# Fastboot tools
# ---------------------------------------------------------------------------

class FastbootTool(BaseTool):
    name: str = "fastboot"
    description: str = (
        "Run a fastboot command. Device must be in fastboot/bootloader mode. "
        "Examples: 'devices', 'getvar all', 'flash boot boot.img', 'reboot', 'oem unlock'."
    )
    args_schema: Type[BaseModel] = _FastbootInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, command: str, serial: str = "", timeout: int = 120) -> str:
        return _fmt(self._client.fastboot(command, serial=serial, timeout=timeout))

    async def _arun(self, command: str, serial: str = "", timeout: int = 120) -> str:
        return self._run(command, serial=serial, timeout=timeout)


class FastbootBootTool(BaseTool):
    name: str = "fastboot_boot"
    description: str = (
        "Boot a kernel image via 'fastboot boot <image_path>' WITHOUT flashing it. "
        "The device must already be in fastboot/bootloader mode (use tac_boot_fastboot first). "
        "Provide 'image_path' as a Windows-accessible path to the boot image. "
        "This can be a local Windows path (e.g. C:\\\\images\\\\boot.img) or a "
        "UNC network path to a build server (e.g. \\\\\\\\buildserver\\\\share\\\\latest\\\\boot.img). "
        "The LLM agent determines the correct image path based on the build or test context."
    )
    args_schema: Type[BaseModel] = _FastbootBootInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, image_path: str, serial: str = "", timeout: int = 120) -> str:
        return _fmt(self._client.fastboot_boot(image_path, serial=serial, timeout=timeout))

    async def _arun(self, image_path: str, serial: str = "", timeout: int = 120) -> str:
        return self._run(image_path, serial=serial, timeout=timeout)


# ---------------------------------------------------------------------------
# Windows shell tool
# ---------------------------------------------------------------------------

class WindowsShellTool(BaseTool):
    name: str = "windows_shell"
    description: str = (
        "Run a command on the Windows host machine via cmd.exe. "
        "Examples: 'dir C:\\firmware', 'ipconfig', 'where adb', 'type C:\\logs\\build.txt'."
    )
    args_schema: Type[BaseModel] = _ShellInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, command: str, timeout: int = 60) -> str:
        return _fmt(self._client.shell(command, timeout=timeout))

    async def _arun(self, command: str, timeout: int = 60) -> str:
        return self._run(command, timeout=timeout)


# ---------------------------------------------------------------------------
# Serial log capture tool
# ---------------------------------------------------------------------------

class CaptureSerialLogTool(BaseTool):
    name: str = "capture_serial_log"
    description: str = (
        "Capture serial port log output from the device and save it to a file on the Linux machine. "
        "Specify 'output_path' (absolute Linux path) and 'duration' in seconds."
    )
    args_schema: Type[BaseModel] = _CaptureSerialLogInput
    _windows_host: str = ""
    _serial_log_port: int = 9001
    _auth_token: str = ""

    def __init__(self, windows_host: str, serial_log_port: int = 9001, auth_token: str = ""):
        super().__init__()
        object.__setattr__(self, "_windows_host", windows_host)
        object.__setattr__(self, "_serial_log_port", serial_log_port)
        object.__setattr__(self, "_auth_token", auth_token)

    def _run(self, output_path: str, duration: float = 30.0,
             include_timestamps: bool = True, raw_only: bool = False) -> str:
        import sys, os
        sys.path.insert(0, os.path.dirname(__file__))
        from serial_log_receiver import SerialLogReceiver
        rx = SerialLogReceiver(host=self._windows_host, serial_log_port=self._serial_log_port,
                               auth_token=self._auth_token)
        try:
            rx.save_to_file(path=output_path, duration=duration,
                            include_timestamps=include_timestamps, raw_only=raw_only)
            try:
                with open(output_path, "r", encoding="utf-8") as fh:
                    line_count = sum(1 for _ in fh)
            except OSError:
                line_count = 0
            return (f"Status: ok\nSerial log saved to: {output_path}\n"
                    f"Lines captured: {line_count}\nDuration: {duration}s")
        except Exception as exc:  # noqa: BLE001
            return f"Status: error\nFailed to capture serial log: {exc}"

    async def _arun(self, output_path: str, duration: float = 30.0,
                    include_timestamps: bool = True, raw_only: bool = False) -> str:
        return self._run(output_path, duration, include_timestamps, raw_only)


# ---------------------------------------------------------------------------
# TACDev info tools
# ---------------------------------------------------------------------------

class TacListDevicesTool(BaseTool):
    name: str = "tac_list_devices"
    description: str = (
        "List all TAC devices connected to the Windows machine. "
        "Returns each device's index, VTP number, port name, and serial number. "
        "Use this to find the correct VTP number or device index."
    )
    args_schema: Type[BaseModel] = _EmptyInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, **kwargs) -> str:
        return _fmt(self._client.tac_list_devices())

    async def _arun(self, **kwargs) -> str:
        return self._run(**kwargs)


class TacGetVersionsTool(BaseTool):
    name: str = "tac_get_versions"
    description: str = "Get the Alpaca and TAC firmware version strings from the Windows machine."
    args_schema: Type[BaseModel] = _EmptyInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, **kwargs) -> str:
        return _fmt(self._client.tac_get_versions())

    async def _arun(self, **kwargs) -> str:
        return self._run(**kwargs)


class TacGetInfoTool(BaseTool):
    name: str = "tac_get_info"
    description: str = (
        "Get info for a TAC device: VTP number, port name, serial number, battery state. "
        "Uses the server-configured device by default. "
        "Specify vtp_number or device_number to target a different device."
    )
    args_schema: Type[BaseModel] = _TacInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return _fmt(self._client.tac_get_info(
            vtp_number=vtp_number or None,
            device_number=device_number if device_number >= 0 else None,
        ))

    async def _arun(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return self._run(vtp_number=vtp_number, device_number=device_number)


class TacGetStatusTool(BaseTool):
    name: str = "tac_get_status"
    description: str = (
        "Get runtime status of a TAC device: USB state, charging state, "
        "battery state, temperature, voltage, and current."
    )
    args_schema: Type[BaseModel] = _TacInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return _fmt(self._client.tac_get_status(
            vtp_number=vtp_number or None,
            device_number=device_number if device_number >= 0 else None,
        ))

    async def _arun(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return self._run(vtp_number=vtp_number, device_number=device_number)


# ---------------------------------------------------------------------------
# TACDev power control tools
# ---------------------------------------------------------------------------

class TacPowerOffTool(BaseTool):
    name: str = "tac_power_off"
    description: str = (
        "Power OFF the device using the TAC controller (PowerOffButton). "
        "The server sleeps 5 seconds after the call. "
        "Specify vtp_number to target a specific device."
    )
    args_schema: Type[BaseModel] = _TacInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return _fmt(self._client.tac_power_off(
            vtp_number=vtp_number or None,
            device_number=device_number if device_number >= 0 else None,
            sleep=sleep if sleep >= 0 else None,
        ))

    async def _arun(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return self._run(vtp_number=vtp_number, device_number=device_number, sleep=sleep)


class TacPowerOnTool(BaseTool):
    name: str = "tac_power_on"
    description: str = (
        "Power ON the device using the TAC controller (PowerOnButton). "
        "The server sleeps 5 seconds after the call to allow the device to boot. "
        "Specify vtp_number to target a specific device."
    )
    args_schema: Type[BaseModel] = _TacInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return _fmt(self._client.tac_power_on(
            vtp_number=vtp_number or None,
            device_number=device_number if device_number >= 0 else None,
            sleep=sleep if sleep >= 0 else None,
        ))

    async def _arun(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return self._run(vtp_number=vtp_number, device_number=device_number, sleep=sleep)


class TacPowerCycleTool(BaseTool):
    name: str = "tac_power_cycle"
    description: str = (
        "Power cycle (hard reboot) the device: PowerOffButton -> sleep 5s -> PowerOnButton -> sleep 5s. "
        "Use this for a complete hardware reset. "
        "Specify vtp_number to target a specific device."
    )
    args_schema: Type[BaseModel] = _TacInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return _fmt(self._client.tac_power_cycle(
            vtp_number=vtp_number or None,
            device_number=device_number if device_number >= 0 else None,
            sleep=sleep if sleep >= 0 else None,
        ))

    async def _arun(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return self._run(vtp_number=vtp_number, device_number=device_number, sleep=sleep)


# ---------------------------------------------------------------------------
# TACDev boot mode tools
# ---------------------------------------------------------------------------

class TacBootEdlTool(BaseTool):
    name: str = "tac_boot_edl"
    description: str = (
        "Boot the device into EDL (Emergency Download / 9008) mode via BootToEDLButton(). "
        "The server sleeps 5 seconds after the call. "
        "Use this before running Qualcomm flashing tools (QFIL, QDART, etc.)."
    )
    args_schema: Type[BaseModel] = _TacInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return _fmt(self._client.tac_boot_edl(
            vtp_number=vtp_number or None,
            device_number=device_number if device_number >= 0 else None,
            sleep=sleep if sleep >= 0 else None,
        ))

    async def _arun(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return self._run(vtp_number=vtp_number, device_number=device_number, sleep=sleep)


class TacBootFastbootTool(BaseTool):
    name: str = "tac_boot_fastboot"
    description: str = (
        "Boot the device into Fastboot mode via BootToFastBootButton(). "
        "The server sleeps 5 seconds after the call. "
        "Use this before running fastboot flash/erase commands."
    )
    args_schema: Type[BaseModel] = _TacInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return _fmt(self._client.tac_boot_fastboot(
            vtp_number=vtp_number or None,
            device_number=device_number if device_number >= 0 else None,
            sleep=sleep if sleep >= 0 else None,
        ))

    async def _arun(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return self._run(vtp_number=vtp_number, device_number=device_number, sleep=sleep)


class TacBootUefiTool(BaseTool):
    name: str = "tac_boot_uefi"
    description: str = (
        "Boot the device into UEFI menu via BootToUEFIMenuButton(). "
        "The server sleeps 5 seconds after the call."
    )
    args_schema: Type[BaseModel] = _TacInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return _fmt(self._client.tac_boot_uefi(
            vtp_number=vtp_number or None,
            device_number=device_number if device_number >= 0 else None,
            sleep=sleep if sleep >= 0 else None,
        ))

    async def _arun(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return self._run(vtp_number=vtp_number, device_number=device_number, sleep=sleep)


class TacBootSecondaryEdlTool(BaseTool):
    name: str = "tac_boot_secondary_edl"
    description: str = (
        "Boot the device into secondary EDL mode via BootToSecondaryEDLButton(). "
        "The server sleeps 5 seconds after the call."
    )
    args_schema: Type[BaseModel] = _TacInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return _fmt(self._client.tac_boot_secondary_edl(
            vtp_number=vtp_number or None,
            device_number=device_number if device_number >= 0 else None,
            sleep=sleep if sleep >= 0 else None,
        ))

    async def _arun(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return self._run(vtp_number=vtp_number, device_number=device_number, sleep=sleep)


class TacBootRecoveryTool(BaseTool):
    name: str = "tac_boot_recovery"
    description: str = (
        "Boot the device into Recovery mode. "
        "The server sleeps 5 seconds after the call."
    )
    args_schema: Type[BaseModel] = _TacInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return _fmt(self._client.tac_boot_recovery(
            vtp_number=vtp_number or None,
            device_number=device_number if device_number >= 0 else None,
            sleep=sleep if sleep >= 0 else None,
        ))

    async def _arun(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return self._run(vtp_number=vtp_number, device_number=device_number, sleep=sleep)


class TacBootNormalTool(BaseTool):
    name: str = "tac_boot_normal"
    description: str = (
        "Boot the device into Normal (Android) mode. "
        "The server sleeps 5 seconds after the call."
    )
    args_schema: Type[BaseModel] = _TacInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return _fmt(self._client.tac_boot_normal(
            vtp_number=vtp_number or None,
            device_number=device_number if device_number >= 0 else None,
            sleep=sleep if sleep >= 0 else None,
        ))

    async def _arun(self, vtp_number: str = "", device_number: int = -1, sleep: float = -1.0) -> str:
        return self._run(vtp_number=vtp_number, device_number=device_number, sleep=sleep)


class TacSetUsbTool(BaseTool):
    name: str = "tac_set_usb"
    description: str = (
        "Enable (state=true) or disable (state=false) the USB connection to the device "
        "via the TAC controller."
    )
    args_schema: Type[BaseModel] = _TacSetUsbInput
    _client: object = None

    def __init__(self, client):
        super().__init__()
        object.__setattr__(self, "_client", client)

    def _run(self, state: bool, vtp_number: str = "", device_number: int = -1) -> str:
        return _fmt(self._client.tac_set_usb(
            state=state,
            vtp_number=vtp_number or None,
            device_number=device_number if device_number >= 0 else None,
        ))

    async def _arun(self, state: bool, vtp_number: str = "", device_number: int = -1) -> str:
        return self._run(state=state, vtp_number=vtp_number, device_number=device_number)


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def make_tools(
    client,
    windows_host: str,
    serial_log_port: int = 9001,
    auth_token: str = "",
    include: Optional[List[str]] = None,
) -> list:
    """
    Create and return a list of LangChain-compatible tools.

    Parameters
    ----------
    client : BridgeClient
        A connected BridgeClient instance.
    windows_host : str
        IP address of the Windows machine (used by CaptureSerialLogTool).
    serial_log_port : int
        TCP port for the serial log stream (default 9001).
    auth_token : str
        Auth token for the serial log port (default "").
    include : list of str or None
        If provided, only include tools whose ``name`` is in this list.
        If None, all tools are included.

    Returns
    -------
    list of BaseTool
    """
    if not _LANGCHAIN_AVAILABLE:
        raise ImportError(
            "LangChain is not installed. Run: pip install -r requirements_linux.txt"
        )

    all_tools = [
        # General
        PingTool(client),
        ListDevicesTool(client),
        # ADB
        AdbTool(client),
        AdbPushTool(client),
        AdbPullTool(client),
        # Fastboot
        FastbootTool(client),
        FastbootBootTool(client),
        # Windows host
        WindowsShellTool(client),
        # Serial logs
        CaptureSerialLogTool(windows_host=windows_host,
                             serial_log_port=serial_log_port,
                             auth_token=auth_token),
        # TACDev info
        TacListDevicesTool(client),
        TacGetVersionsTool(client),
        TacGetInfoTool(client),
        TacGetStatusTool(client),
        # TACDev power
        TacPowerOffTool(client),
        TacPowerOnTool(client),
        TacPowerCycleTool(client),
        # TACDev boot modes
        TacBootEdlTool(client),
        TacBootFastbootTool(client),
        TacBootUefiTool(client),
        TacBootSecondaryEdlTool(client),
        TacBootRecoveryTool(client),
        TacBootNormalTool(client),
        # TACDev USB
        TacSetUsbTool(client),
    ]

    if include:
        all_tools = [t for t in all_tools if t.name in include]

    return all_tools
