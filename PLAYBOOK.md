# Copilot Chat 实战操作手册

本手册面向"使用 GitHub Copilot Chat + mcp-device-gateway 进行嵌入式/Linux 设备排障与开发调试"的场景，按常见任务提供可直接复用的工具调用剧本。

> **前置条件**：已完成 [README.md](README.md) 第 6、7 节的安装与接入配置，Copilot Chat 可调用 `embedded-device-gateway` 下的工具。

---

## 剧本一：设备连通性验证

**适用时机**：刚接通电源、更换网段、SSH 密钥变更后。

### 1.1 列出已配置设备

```
@embedded-device-gateway #device_list
```

工具调用：

```json
device_list()
```

预期返回：

```json
[
  {
    "name": "devkit-01",
    "host": "192.168.200.218",
    "port": 22,
    "username": "root",
    "allowed_roots": ["/opt/app/", "/tmp/"]
  }
]
```

### 1.2 测试 SSH 连通性

```
@embedded-device-gateway 检查 devkit-01 是否在线
```

工具调用：

```json
device_ping(device_name="devkit-01")
```

预期返回（在线）：

```json
{"device": "devkit-01", "reachable": true}
```

---

## 剧本二：快速健康检查

**适用时机**：设备异常告警时的第一步排查。

### 2.1 系统信息与运行时长

```
@embedded-device-gateway 检查 devkit-01 的基本系统状态
```

工具调用：

```json
cmd_exec(device_name="devkit-01", command_key="health_check")
```

预期 stdout（示例）：

```
Linux devkit 4.9.1 #1 SMP Thu Jan 1 00:00:00 UTC 2026 aarch64 GNU/Linux
 07:52:12 up 3 days,  2:14,  1 user,  load average: 0.02, 0.01, 0.00
```

### 2.2 磁盘使用情况

```
@embedded-device-gateway 查看 devkit-01 的磁盘使用率
```

工具调用：

```json
cmd_exec(device_name="devkit-01", command_key="disk_usage")
```

### 2.3 内存使用情况

```
@embedded-device-gateway 查看 devkit-01 的内存余量
```

工具调用：

```json
cmd_exec(device_name="devkit-01", command_key="mem_usage")
```

---

## 剧本三：应用服务排查与恢复

**适用时机**：应用无响应、服务频繁崩溃、watchdog 触发后。

### 3.1 查看服务状态（以 idm 为例）

```
@embedded-device-gateway 查看 devkit-01 上 idm 服务的状态
```

工具调用：

```json
cmd_exec(device_name="devkit-01", command_key="idm_status")
```

### 3.2 查看进程是否在运行

```
@embedded-device-gateway 检查 devkit-01 的 idm 进程是否存在
```

工具调用：

```json
cmd_exec(device_name="devkit-01", command_key="idm_process")
```

预期输出（进程在运行时）：

```
root  1234  0.0  0.2  12345  2345 ?  Ssl  07:00   0:01 ./displacementMeter.c308x
```

### 3.3 重启服务

> **注意**：重启前建议先拉取日志（见剧本四），以保留崩溃现场。

```
@embedded-device-gateway 重启 devkit-01 上的 idm 服务
```

工具调用：

```json
cmd_exec(device_name="devkit-01", command_key="restart_idm")
```

重启后立即复验：

```json
cmd_exec(device_name="devkit-01", command_key="idm_status")
```

---

## 剧本四：日志拉取与分析

**适用时机**：程序崩溃、输出异常、需要进行离线日志分析时。

### 4.1 查看日志目录结构

```
@embedded-device-gateway 列出 devkit-01 的 idm 日志目录
```

工具调用：

```json
cmd_exec(device_name="devkit-01", command_key="idm_log_list")
```

### 4.2 下载日志文件到本地

```
@embedded-device-gateway 把 devkit-01 上的 /opt/idm/log/app.log 下载到本地 D:/tmp/app.log
```

工具调用：

```json
file_download(
  device_name="devkit-01",
  remote_path="/opt/idm/log/app.log",
  local_path="D:/tmp/app.log"
)
```

下载成功后，直接让 Copilot Chat 分析日志内容：

```
刚下载的 D:/tmp/app.log 里有没有 ERROR 或崩溃关键字？列出最近 20 条异常记录。
```

> Copilot Chat 可以直接读取本地文件并进行内容分析，无需额外工具。

### 4.3 批量拉取多个日志

同一会话中可连续调用，Agent 会按顺序执行：

```
@embedded-device-gateway 分别下载 devkit-01 上的 /opt/idm/log/app.log 和 /opt/idm/log/error.log 到 D:/tmp/ 目录
```

---

## 剧本五：文件部署与版本替换

**适用时机**：交叉编译后需要将新固件或脚本下发到设备验证。

### 5.1 上传新版二进制

```
@embedded-device-gateway 把本地 D:/build/displacementMeter.c308x 上传到 devkit-01 的 /tmp/ 目录
```

工具调用：

```json
file_upload(
  device_name="devkit-01",
  local_path="D:/build/displacementMeter.c308x",
  remote_path="/tmp/displacementMeter.c308x"
)
```

### 5.2 验证上传结果

```
@embedded-device-gateway 列出 devkit-01 的 /tmp/ 目录，确认文件已上传
```

工具调用：

```json
cmd_exec(device_name="devkit-01", command_key="list_tmp_dir")
```

### 5.3 上传配置文件并检查

```
@embedded-device-gateway 上传 D:/prj/config/app.conf 到 devkit-01 的 /tmp/app.conf，然后列出 /tmp/ 确认
```

---

## 剧本六：构建与编译调试

**适用时机**：远端设备有本地构建环境（如 Makefile），需要触发编译并收集日志。

### 6.1 执行 make 目标

```
@embedded-device-gateway 在 devkit-01 上执行 make clean
```

工具调用：

```json
cmd_exec(
  device_name="devkit-01",
  command_key="run_make",
  args=["clean"]
)
```

```
@embedded-device-gateway 在 devkit-01 上执行 make all，超时设为 120 秒
```

工具调用：

```json
cmd_exec(
  device_name="devkit-01",
  command_key="run_make",
  args=["all"],
  timeout_sec=120
)
```

### 6.2 编译失败时下载构建日志

```
@embedded-device-gateway 下载 devkit-01 的 /opt/idm/build.log 到本地 D:/tmp/build.log
```

再让 Copilot Chat 诊断：

```
分析 D:/tmp/build.log，找出编译错误原因并建议修复方案
```

---

## 剧本七：综合排障流程（完整示例）

以下是一个完整的对话流程示例，展示 Copilot Chat 如何串联多个工具完成排障任务。

```
用户：devkit-01 的应用无响应，帮我排查一下
```

Agent 会依次执行：

1. `device_ping(device_name="devkit-01")` — 确认设备在线
2. `cmd_exec(..., command_key="idm_process")` — 检查进程
3. `cmd_exec(..., command_key="idm_status")` — 查看服务状态
4. `cmd_exec(..., command_key="disk_usage")` — 排除磁盘满
5. `cmd_exec(..., command_key="mem_usage")` — 排除内存不足
6. `file_download(..., remote_path="/opt/idm/log/app.log", ...)` — 拉取日志
7. 分析日志，给出原因与重启建议

---

## 工具参数速查

| 工具 | 必填参数 | 可选参数 |
|---|---|---|
| `device_list` | — | — |
| `device_ping` | `device_name` | — |
| `cmd_exec` | `device_name`, `command_key` | `args`, `timeout_sec`（默认 30） |
| `file_upload` | `device_name`, `local_path`, `remote_path` | — |
| `file_download` | `device_name`, `remote_path`, `local_path` | — |

## 错误速查

| 错误信息 | 原因 | 处理方法 |
|---|---|---|
| `Unknown device 'xxx'` | device_name 与配置不符 | 先调用 `device_list` 确认名称 |
| `Unknown command template 'xxx'` | command_key 不在 command_templates 中 | 检查 devices.yaml 配置 |
| `Unsafe argument 'xxx'` | args 中含有禁止字符 | args 只允许 `a-z A-Z 0-9 _ . / : = + -` |
| `Remote path is not allowed` | 目标路径不在 allowed_roots 中 | 检查设备的 allowed_roots 配置 |
| `reachable: false` | SSH 连通失败 | 检查网络、端口、账号、密钥/密码 |
