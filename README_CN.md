# 嵌入式设备自动化框架 — 使用手册

本框架让运行在 **Linux 机器**上的脚本或 LLM Agent，通过 TCP 远程控制连接到 **Windows 机器**的嵌入式设备，支持 ADB 命令、fastboot 命令、TAC 电源控制以及串口日志实时采集。

```
┌─────────────────────────────────────────────────────────────┐
│                      Linux 机器                              │
│  example_agent.py / sequence_runner.py / 自定义脚本          │
│  BridgeClient (client/linux_client.py)                      │
│  SerialLogReceiver (client/serial_log_receiver.py)          │
└──────────────────────┬──────────────────────────────────────┘
                       │  TCP 9000（命令）/ TCP 9001（串口流）
┌──────────────────────┼──────────────────────────────────────┐
│                    Windows 机器                              │
│  windows_bridge_server.py                                   │
│  ├── CommandHandler  → adb.exe / fastboot.exe / cmd.exe     │
│  └── SerialLogManager → COM 口读取 + 广播                    │
│                       │ USB                                 │
│              嵌入式设备（ADB / fastboot / 串口）              │
└─────────────────────────────────────────────────────────────┘
```

---

## 目录

1. [环境准备](#1-环境准备)
2. [启动 Bridge Server（Windows）](#2-启动-bridge-serverwindows)
3. [demo 模式 — 连通性验证](#3-demo-模式--连通性验证)
4. [debug 模式 — TAC 电源、启动模式与脚本执行](#4-debug-模式--tac-电源启动模式与脚本执行)
5. [cycle-log 模式 — 掉电重启并采集串口日志](#5-cycle-log-模式--掉电重启并采集串口日志)
6. [sequence_runner — 自动化刷机序列](#6-sequence_runner--自动化刷机序列)
7. [bisect_runner — 二分查找问题提交](#7-bisect_runner--二分查找问题提交)
8. [配置文件说明](#8-配置文件说明)
9. [常见问题](#9-常见问题)

---

## 1. 环境准备

### Windows 机器

| 前提条件 | 说明 |
|----------|------|
| Python 3.10+ | 安装并加入 PATH |
| adb.exe / fastboot.exe | 来自 Android Platform Tools，加入 PATH |
| TACDev 驱动 | TAC 电源控制板驱动，需提前安装 |
| 串口已识别 | 在设备管理器中确认 COM 口编号（如 `COM9`） |

```cmd
pip install -r requirements_windows.txt
```

编辑 `server/config.yaml`，填写实际 COM 口和波特率：

```yaml
serial:
  port: "COM9"
  baudrate: 115200
tacdev:
  vtp_number: ""          # 留空则自动选第一个设备
  post_action_sleep: 5
```

### Linux 机器

```bash
pip install -r requirements_linux.txt
```

编辑 `client/config.yaml`，填写 Windows 机器的 IP：

```yaml
bridge:
  host: "10.64.68.116"   # Windows 机器 IP
  command_port: 9000
  serial_log_port: 9001
  auth_token: ""
```

---

## 2. 启动 Bridge Server（Windows）

在 Windows 机器上执行：

```cmd
python server/windows_bridge_server.py
```

正常启动后输出：

```
Windows Bridge Server started
  Command port    : 0.0.0.0:9000
  Serial log port : 0.0.0.0:9001
  Serial device   : COM9 @ 115200 baud
```

> **防火墙**：在 Windows 防火墙中放行 TCP 入站端口 9000 和 9001。

---

## 3. demo 模式 — 连通性验证

`demo` 模式不需要 LLM，用于快速验证 Bridge Server 是否正常工作，依次执行：ping、列出 ADB/fastboot 设备、运行 ADB shell 命令、采集 10 秒串口日志。

```bash
python example_agent.py --host 10.64.68.116 --mode demo
```

**预期输出示例：**

```
[1] Connecting to bridge server at 10.64.68.116:9000 …
[2] Ping …
    {status: ok, message: pong}
[3] Listing ADB + fastboot devices …
    ADB devices: ...
[4] Running: adb shell getprop ro.build.version.release …
    Android version: 14
[8] Capturing 10 seconds of serial logs …
    Captured 42 lines → /tmp/device_serial_demo.log
```

---

## 4. debug 模式 — TAC 电源、启动模式与脚本执行

`debug` 模式通过 TACDev 控制开发板电源和启动模式，也可直接在 Windows 主机上执行脚本，支持以下动作：

| `--tac-action` | 说明 |
|----------------|------|
| `on`       | 上电 |
| `off`      | 下电 |
| `cycle`    | 掉电重启（off → 等待 → on） |
| `status`   | 查看设备信息和运行状态 |
| `list`     | 列出所有 TAC 设备 |
| `edl`      | 进入 EDL（紧急下载）模式 |
| `fastboot` | 进入 Fastboot 模式 |
| `normal`   | 正常启动（Android / Linux） |
| `recovery` | 进入 Recovery 模式 |
| `shell`    | 在 Windows 主机上执行脚本或命令（配合 `--shell-script` 使用） |
| `execute_action` | 在 Windows 主机上执行 cmd 命令（命令直接跟在动作后面作为位置参数） |

### 4.1 电源与启动模式

```bash
# 列出所有 TAC 设备
python example_agent.py --host 10.64.68.116 --mode debug --tac-action list

# 查看设备状态
python example_agent.py --host 10.64.68.116 --mode debug --tac-action status

# 上电
python example_agent.py --host 10.64.68.116 --mode debug --tac-action on

# 下电
python example_agent.py --host 10.64.68.116 --mode debug --tac-action off

# 掉电重启
python example_agent.py --host 10.64.68.116 --mode debug --tac-action cycle

# 进入 EDL 模式（用于 emmcdl / QFIL 刷机）
python example_agent.py --host 10.64.68.116 --mode debug --tac-action edl

# 进入 Fastboot 模式
python example_agent.py --host 10.64.68.116 --mode debug --tac-action fastboot

# 正常启动
python example_agent.py --host 10.64.68.116 --mode debug --tac-action normal

# 进入 Recovery 模式
python example_agent.py --host 10.64.68.116 --mode debug --tac-action recovery
```

**预期输出示例（cycle）：**

```
  TACDev Debug – action: cycle
[TAC] Power cycling the board (off -> sleep -> on) …
    Serial 9800020711120224: power cycle done.
status: ok
```

**预期输出示例（edl）：**

```
  TACDev Debug – action: edl
[TAC] Booting to EDL mode …
    Serial 9800020711120224: boot to EDL done. Slept 5.0s.
status: ok
```

**预期输出示例（fastboot）：**

```
  TACDev Debug – action: fastboot
[TAC] Booting to Fastboot mode …
    Serial 9800020711120224: boot to Fastboot done. Slept 5.0s.
status: ok
```

### 4.2 在 Windows 主机上执行脚本（shell）

`--tac-action shell` 通过 Bridge Server 在 Windows 主机上执行任意命令或 `.bat` 脚本，stdout / stderr 实时回传到 Linux 终端，超时默认 300 秒。

```bash
# 执行 .bat 刷机脚本
python example_agent.py --host 10.64.68.116 --mode debug \
    --tac-action shell \
    --shell-script "fastboot_shell246_systemmainline.bat"

# 执行单条 Windows 命令
python example_agent.py --host 10.64.68.116 --mode debug \
    --tac-action shell \
    --shell-script "fastboot devices"

# 查询 adb 路径
python example_agent.py --host 10.64.68.116 --mode debug \
    --tac-action shell \
    --shell-script "where adb"
```

| 参数 | 说明 |
|------|------|
| `--shell-script` | 要在 Windows 主机上执行的命令或脚本文件名（必填，仅 `shell` 动作使用） |

**预期输出示例（执行 .bat 脚本）：**

```
  TACDev Debug – action: shell

[SHELL] Running on Windows host: fastboot_shell246_systemmainline.bat …
Z:\yyj\...>emmcdl_lun7.exe ...
Version 2.43
...
Completed: 100%, Transferred 302MB in 8.1 secs
status: ok  returncode: 0
```

> **提示**：`shell` 动作不会自动切换启动模式，如需先进入 EDL 再刷机，请先执行 `--tac-action edl`，再执行 `--tac-action shell`，或使用 `sequence_runner` 将多步骤串联。

### 4.3 在 Windows 主机上执行命令（execute_action）

`--tac-action execute_action` 与 `shell` 功能相同，区别在于命令直接作为位置参数跟在动作后面，无需 `--shell-script`，适合带较多参数的工具调用（如 emmcdl 刷写单个分区）。超时默认 300 秒。

```bash
# 使用 EMMCDL 向 SPI-NOR 刷写 uefi_dtb_a 分区
python example_agent.py --host 10.64.68.116 --mode debug --tac-action execute_action 'C:\Users\yuanjiey\Downloads\EMMCDL.exe -f Z:\yyj\test\images\spinor-cdt\xbl_s_devprg_ns.melf -memoryname spinor -p COM14 -b uefi_dtb_a Y:\Hamoa_IOT.UBUN.1.0.r1-00116-STD.INT.DESKTOP-1\common\build\bin\HAMOA_bootbinaries\spinor\uefi_dtbs_gunyah.xz'

# 执行单条 Windows 命令
python example_agent.py --host 10.64.68.116 --mode debug --tac-action execute_action "ipconfig /all"
```

> **注意**：在 Linux shell 中请用**单引号**包裹含 Windows 路径的命令，避免反斜杠 `\` 被 bash 转义。刷机前需先通过 `--tac-action edl` 让设备进入 EDL 模式，并确认 `-p` 指定的 COM 口与设备一致。

---

## 5. cycle-log 模式 — 掉电重启并采集串口日志

`cycle-log` 模式在掉电重启前先订阅串口流，确保不遗漏任何启动日志，然后等待指定时长后保存日志文件。

```bash
python example_agent.py \
    --host 10.64.68.116 \
    --mode cycle-log \
    --log-duration 120 \
    --log-output /tmp/serial_logs/boot_log.txt
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `192.168.1.100` | Windows Bridge Server IP |
| `--log-duration` | `60` | 采集时长（秒） |
| `--log-output` | `/tmp/serial_logs/boot_log.txt` | 日志保存路径（Linux 本地） |

**执行流程：**

1. 连接串口流（TCP 9001），后台线程开始采集
2. 通过 TCP 9000 发送 `tac_power_cycle` 命令
3. 等待 `--log-duration` 秒（或按 Ctrl+C 提前结束）
4. 将采集到的日志写入 `--log-output`

**预期输出示例：**

```
  Cycle + Serial Log Capture
  host     : 10.64.68.116
  duration : 120s
  output   : /tmp/serial_logs/boot_log.txt
[TAC] Power cycling the board …
  S - QC_IMAGE_VERSION_STRING=BOOT.MXF.1.0...
  B - SBL1, Start
  ...
  Lines captured : 856
  Log saved to   : /tmp/serial_logs/boot_log.txt
```

> **提示**：如需在重启前后持续采集（串口流中断后自动重连），请改用 `sequence_runner`（见下节）。

---

## 6. sequence_runner — 自动化刷机序列

`sequence_runner` 按照序列 YAML 文件中定义的步骤顺序执行，支持串口采集自动重连、编译、刷机、电源控制等完整流程。

### 6.1 运行

```bash
python client/sequence_runner.py --host 10.64.68.116
```

可选参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `192.168.1.100` | Windows Bridge Server IP |
| `--sequence` | `client/sequence/base/sequence.yaml` | 序列配置文件路径 |
| `--config` | `client/config.yaml` | 客户端配置文件路径 |
python client/sequence_runner.py --host 10.64.68.116 --sequence client/sequence/base/sequence_reboot_uart.yaml

### 6.2 序列文件目录结构

```
client/sequence/
├── base/
│   ├── sequence.yaml                    # 默认刷机序列（含编译）
│   └── sequence_without_compile.yaml    # 不含编译步骤
└── bisect/                   # bisect_runner 自动生成的临时序列
    ├── bisect_template.yaml
    └── <commit_id>.yaml   # 每个被测提交自动生成
```

### 6.3 sequence.yaml 结构

每个步骤包含以下字段：

```yaml
- id: <步骤名称>          # 日志中显示的唯一标识
  action: <动作>          # 见下方支持的动作列表
  params:                 # 动作参数（可选）
    ...
  on_fail: abort          # 失败时行为：abort（默认）或 continue
```

### 6.4 支持的动作

| 动作 | 说明 | 常用参数 |
|------|------|----------|
| `read_uart` | 后台开始采集串口日志 | `output`: 日志保存路径 |
| `stop_uart` | 停止采集并保存日志 | `duration`: 等待时长（秒） |
| `power_off` | TAC 下电 | `sleep`: 下电后等待秒数 |
| `power_on` | TAC 上电 | `sleep`: 上电后等待秒数 |
| `power_cycle` | TAC 掉电重启 | `sleep`: 重启后等待秒数 |
| `boot_edl` | TAC 进入 EDL 模式 | `sleep`: 等待秒数 |
| `boot_fastboot` | TAC 进入 Fastboot 模式 | `sleep`: 等待秒数 |
| `boot_normal` | TAC 正常启动 | `sleep`: 等待秒数 |
| `boot_recovery` | TAC 进入 Recovery 模式 | `sleep`: 等待秒数 |
| `fastboot` | 在 Windows 上执行 fastboot 命令 | `command`, `serial`, `timeout` |
| `fastboot_boot` | fastboot boot（临时启动，不刷写） | `image_path`, `timeout` |
| `adb` | 执行 ADB 命令 | `command`, `serial`, `timeout` |
| `adb_push` | 推送文件到设备 | `local`, `remote`, `timeout` |
| `adb_pull` | 从设备拉取文件 | `remote`, `local`, `timeout` |
| `windows_shell` | 在 **Windows** 上执行 cmd 命令或脚本 | `command`, `timeout`, 见扩展参数 |
| `linux_shell` | 在 **Linux 客户端本地**执行 bash 命令 | `command`, `timeout` |
| `wait` | 等待固定秒数 | `seconds` |
| `ping` | 检查 Bridge Server 连通性 | — |

> **注意**：`windows_shell` 通过 Bridge Server 转发到 Windows 的 `cmd.exe` 执行；`linux_shell` 直接在本地 `/bin/bash` 中执行，不经过网络。

### 6.5 windows_shell / linux_shell 扩展参数

针对 `emmcdl` 等工具即使成功也会输出 stderr 并返回非零退出码的情况，`windows_shell` 动作提供三个扩展参数：

| 参数 | 类型 | 说明 |
|------|------|------|
| `success_patterns` | 字符串列表 | stdout/stderr 中匹配任意一条则视为成功 |
| `allowed_returncodes` | 整数列表 | 列表中的退出码视为成功（如 `[0, 1]`） |
| `ignore_stderr` | bool | `true` 时无论退出码如何均视为成功，stderr 仅记录日志 |

### 6.6 完整示例序列（sequence.yaml）

```yaml
sequence:

  - id: read_uart
    action: read_uart
    params:
      output: "/tmp/serial_logs/flash_session.log"

  - id: compile_repo
    action: linux_shell
    params:
      command: >-
        cd /path/to/linux-sheepdog &&
        python3 sheepdog-slave.py --repo-config template-slave-qli.ini
        --local=/path/to/kernel --compile-config qli-config
        --board-config talos_config.yaml --build_only
      timeout: 3600
    on_fail: abort

  - id: power_off
    action: power_off
    params:
      sleep: 5

  - id: boot_edl
    action: boot_edl
    params:
      sleep: 15

  - id: flash_image
    action: windows_shell
    params:
      command: "talos_shell246.bat"
      timeout: 180
    on_fail: abort

  - id: power_on
    action: power_on
    params:
      sleep: 10

  - id: record_log
    action: stop_uart
    params:
      duration: 60

  - id: power_off
    action: power_off
    params:
      sleep: 5
```

### 6.7 串口采集自动重连机制

`read_uart` 启动的后台线程在串口流断开（如掉电期间服务端关闭连接）后，会自动等待 3 秒并重新连接，直到 `stop_uart` 发出停止信号。

```
[read_uart 启动] → 连接 TCP 9001 → 采集日志
                                  ↓ 断线（掉电）
                         等待 3s → 重新连接 → 继续采集
                                  ↓ stop_uart 触发
                         停止采集 → 保存日志文件
```

---

## 7. bisect_runner — 二分查找问题提交

`bisect_runner` 在一组 git 提交上执行二分查找：每次取中间提交，自动生成序列文件、调用 `sequence_runner` 完成编译+刷机+采集，然后由用户判断结果，逐步缩小范围，最终定位引入问题的提交。

### 7.1 运行

```bash
python client/bisect_runner.py --host 10.64.68.116
```

可选参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | 必填 | Windows Bridge Server IP |
| `--begin_commit` | 列表中最旧提交 | 已知正常的起始提交 ID |
| `--end_commit` | 列表中最新提交 | 已知异常的结束提交 ID |
| `--list` | — | 打印提交列表后退出 |

### 7.2 工作流程

```
bisect_runner.py
  │
  ├─ 读取 COMMITS 列表（oldest → newest）
  ├─ 二分取中间提交 mid
  │    ├─ 渲染 bisect_template.yaml → client/sequence/bisect/<commit_id>.yaml
  │    │    • git checkout <commit_id>（linux_shell）
  │    │    • 编译内核（linux_shell，sheepdog-slave.py）
  │    │    • 刷机流程（power_off → boot_edl → windows_shell → power_on）
  │    │    • 串口日志保存到 /tmp/serial_logs/<commit_id>_serial_log
  │    └─ 调用 sequence_runner.py --sequence bisect/<commit_id>.yaml
  ├─ 提示用户输入结果：g=good / b=bad / s=skip / q=quit
  └─ 根据结果缩小范围，重复直到定位第一个坏提交
```

### 7.3 使用示例

```bash
# 完全交互式（每次刷机后手动判断）
python client/bisect_runner.py --host 10.64.68.116

# 指定起止提交
python client/bisect_runner.py --host 10.64.68.116 \
    --begin_commit a1f6d71a69b0 --end_commit 32eb26c08e43

# 查看提交列表
python client/bisect_runner.py --host 10.64.68.116 --list
```

### 7.4 串口日志

每个被测提交的串口日志单独保存，路径格式为：

```
/tmp/serial_logs/<commit_id>_serial_log
```

例如测试提交 `b7cce9a3884a` 时，日志保存到 `/tmp/serial_logs/b7cce9a3884a_serial_log`。

---
## 8. 配置文件说明

### server/config.yaml（Windows 端）

```yaml
server:
  host: "0.0.0.0"
  command_port: 9000
  serial_log_port: 9001
  auth_token: ""          # 留空则不鉴权

serial:
  port: "COM9"            # 实际 COM 口
  baudrate: 115200
  log_file: "serial_output.log"

tacdev:
  vtp_number: ""          # 留空则自动选第一个 TAC 设备
  post_action_sleep: 5    # 电源操作后等待秒数

adb:
  path: "adb"
  default_timeout: 30

fastboot:
  path: "fastboot"
  default_timeout: 120
```

### client/config.yaml（Linux 端）

```yaml
bridge:
  host: "10.64.68.116"   # Windows 机器 IP
  command_port: 9000
  serial_log_port: 9001
  auth_token: ""
  connect_timeout: 10
  recv_timeout: 60
```

---

## 9. 常见问题

**连接被拒绝（Connection refused）**
- 确认 Windows 上 `windows_bridge_server.py` 已启动
- 检查 Windows 防火墙是否放行 TCP 9000 / 9001 入站
- 用 `ipconfig` 确认 Windows 机器 IP 地址

**串口无数据 / 串口打开失败**
- 在设备管理器中确认 COM 口编号，更新 `server/config.yaml`
- 确保没有其他程序（PuTTY、Arduino IDE 等）占用该 COM 口
- 尝试以管理员身份运行 Bridge Server
- 运行 `python diagnose.py`（在 Windows 上）进行诊断

**串口日志为 0 行**
- 串口流在掉电期间断开是正常现象，`sequence_runner` 的 `read_uart` 会自动重连
- 如果使用 `cycle-log` 模式仍为 0 行，检查 `recv_timeout` 是否过短（默认 30 秒）

**emmcdl 刷机失败（Status: 6 / Status: 31）**
- `Status: 31` 通常是第一条 emmcdl 命令完成后设备退出 EDL 模式，第二条命令找不到设备
- 检查 `.bat` 脚本中的命令顺序和 COM 口编号
- 在 `sequence.yaml` 中对 `shell` 步骤设置 `ignore_stderr: true`

**TAC 设备找不到**
- 确认 TACDev 驱动已安装，USB 线已连接
- 运行 `python diagnose.py` 检查 TACDev 是否可以从主线程和后台线程打开
- 在 `server/config.yaml` 中明确填写 `vtp_number`

**LangChain Agent 无法运行**
- 确认已设置环境变量：`export OPENAI_API_KEY="sk-..."`
- 确认已安装依赖：`pip install -r requirements_linux.txt`
