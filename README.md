# mcp-device-gateway

面向嵌入式设备工作流的 MCP 服务。它通过设备清单、命令模板、路径白名单和审计日志，为 Agent 提供受控的 SSH/SFTP 访问能力，而不是开放任意 shell。

## 快速导航

- [README.md](README.md)：项目概览、接入方式、配置说明
- [devices.example.yaml](devices.example.yaml)：通用开发示例配置，无真实地址和口令
- [devices.production.example.yaml](devices.production.example.yaml)：生产环境最小授权模板
- [MCP_CAPABILITIES.md](MCP_CAPABILITIES.md)：工具、资源、提示模板与推荐工作流
- [MCP_SURFACE_QUICKREF.md](MCP_SURFACE_QUICKREF.md)：当前服务面与模板清单速查
- [PLAYBOOK.md](PLAYBOOK.md)：面向 Copilot Chat 的实战操作剧本
- [start-mcp-device-gateway.bat](start-mcp-device-gateway.bat)：Windows 启动脚本
- [build-exe.bat](build-exe.bat)：PyInstaller 打包脚本
- [release.bat](release.bat)：构建并发布包

## 核心能力

- 设备发现：列出设备、查看设备画像、验证 SSH 连通性
- 模板执行：按命令模板执行同步命令、批量并发命令、异步长任务
- 文件操作：上传、下载、列目录、查询远端文件元信息
- 配置辅助：配置摘要资源、任务推荐、设备操作提示模板
- 安全约束：参数正则校验、远端路径白名单、结构化审计日志

当前服务暴露 15 个 MCP 工具、1 个资源和 1 个提示模板。

## 架构概览

```text
src/mcp_device_gateway/
  server.py      FastMCP 入口，定义工具、资源、提示模板和审计流程
  config.py      加载 YAML 配置并冻结为数据类
  security.py    参数校验与远端路径白名单检查
  ssh_client.py  Paramiko 封装，含连接池、执行与 SFTP 能力

devices.example.yaml             通用开发示例配置
devices.production.example.yaml  生产环境最小授权模板
entrypoint.py                    PyInstaller 构建入口
```

请求主链路：工具调用 -> 读取设备配置 -> 参数或路径检查 -> SSH/SFTP 执行 -> 返回结果 -> 追加 JSONL 审计。

## 安全边界

- 只允许执行 command_templates 中声明的模板，不提供任意 shell 入口
- 所有 cmd_exec 参数都通过 `^[a-zA-Z0-9_./:=+\-]+$` 的 fullmatch 校验
- 所有远端文件路径都经过 PurePosixPath 规范化并受 allowed_roots 约束
- 配置了 known_hosts 时使用 RejectPolicy，未配置时才退化为 AutoAddPolicy
- 每次工具调用都写入 JSONL 审计日志，字段包括时间、工具名和关键载荷

## 工具总览

### 发现与规划

- `device_list`：列出设备清单
- `device_profile_get`：查看设备画像、能力标签和推荐模板
- `device_ping`：验证 SSH 连通性
- `command_template_list`：列出全部模板及用途、参数、风险、执行模式
- `command_template_get`：查看单个模板详情
- `capability_overview`：返回能力地图和推荐工作流
- `task_recommend`：根据自然语言任务生成调用草案

### 命令执行

- `cmd_exec`：执行单条同步命令
- `cmd_exec_batch`：并发执行多条独立同步命令
- `cmd_exec_async`：提交长耗时任务，立即返回 `job_id`
- `cmd_exec_result`：轮询异步任务结果

### 文件与目录

- `dir_list`：列出远端目录内容
- `file_stat`：获取远端文件或目录元信息
- `file_upload`：上传本地文件
- `file_download`：下载远端文件

### 资源与提示模板

- `config://summary`：返回当前配置摘要
- `device_ops_prompt(task, device_name?)`：生成推荐操作顺序和工具选择提示

## 推荐工作流

1. 先调用 `device_list`，必要时配合 `device_profile_get` 选择目标设备。
2. 调用 `device_ping` 验证连通性。
3. 对命令类任务，先用 `task_recommend` 或 `command_template_list`，再用 `command_template_get` 确认 `exec_mode`。
4. `exec_mode=sync` 时使用 `cmd_exec`；多条独立短命令使用 `cmd_exec_batch`。
5. `exec_mode=async` 时使用 `cmd_exec_async`，随后调用 `cmd_exec_result` 轮询。
6. 文件场景使用 `dir_list`、`file_stat`、`file_upload`、`file_download`。

## 快速开始

### 安装

```powershell
cd D:/prj/mcp-device-gateway
python -m venv .venv
. .venv/Scripts/Activate.ps1
pip install -e .
```

### 准备配置

建议复制 [devices.example.yaml](devices.example.yaml) 为私有文件，例如 `devices.local.yaml`，再填写真实设备信息。

不建议直接把含真实 IP、账号或口令的配置文件提交到仓库。

### 环境变量

```powershell
$env:MCP_DEVICE_CONFIG = "D:/prj/mcp-device-gateway/devices.local.yaml"
$env:MCP_AUDIT_LOG = "D:/prj/mcp-device-gateway/mcp_audit.log"
$env:MCP_TRANSPORT = "stdio"
```

可选变量：

- `MCP_DEVICE_CONFIG`：配置文件路径，默认 `./devices.example.yaml`
- `MCP_AUDIT_LOG`：审计日志路径，默认 `./mcp_audit.log`
- `MCP_TRANSPORT`：传输层，支持 `stdio`、`sse`、`streamable-http`
- `MCP_CONFIG_POLL_INTERVAL_SEC`：配置轮询间隔，默认 2 秒

### 启动服务

通过脚本启动：

```bat
start-mcp-device-gateway.bat --config devices.local.yaml --transport stdio
```

直接运行 Python 模块：

```bat
python -m mcp_device_gateway.server --config devices.local.yaml
```

独立可执行文件：

```bat
dist\mcp-device-gateway.exe
```

## VS Code MCP 接入

### 通过启动脚本

```json
{
  "servers": {
    "embedded-device-gateway": {
      "type": "stdio",
      "command": "D:\\prj\\mcp-device-gateway\\start-mcp-device-gateway.bat",
      "args": [
        "--config", "D:\\prj\\mcp-device-gateway\\devices.local.yaml",
        "--audit", "D:\\prj\\mcp-device-gateway\\mcp_audit.log",
        "--transport", "stdio"
      ]
    }
  }
}
```

### 通过独立 exe

```json
{
  "servers": {
    "embedded-device-gateway": {
      "type": "stdio",
      "command": "D:\\prj\\mcp-device-gateway\\dist\\mcp-device-gateway.exe",
      "env": {
        "MCP_DEVICE_CONFIG": "D:\\prj\\mcp-device-gateway\\devices.local.yaml",
        "MCP_AUDIT_LOG": "D:\\prj\\mcp-device-gateway\\mcp_audit.log"
      }
    }
  }
}
```

## 配置文件说明

建议将私有设备清单命名为 `devices_*.yaml` 或 `devices.local.yaml`。这些文件默认应只保留在本地，不进入版本控制；仓库中仅保留脱敏示例文件。

### devices

- `host`、`username` 必填
- `allowed_roots: []` 表示不限制远端路径，只建议在可信开发环境使用
- `description`、`when_to_use`、`capabilities`、`tags`、`preferred_templates` 用于提升 Agent 选型准确度
- `os_family` 仅支持 `linux` 或 `windows`，默认 `linux`

### command_templates

支持两种格式，但不能混用：

1. 平铺旧格式

```yaml
command_templates:
  health_check: "uname -a; uptime"
```

1. 分组增强格式

```yaml
command_templates:
  linux_common:
    list_dir:
      template: "ls -al {0}"
      description: "列出目录"
      when_to_use: "检查目录结构"
      args: ["path"]
      examples:
        - ["/opt/app/"]
      risk: "low"
      exec_mode: sync
  device_specific:
    devkit-dev:
      restart_app:
        template: "systemctl restart app"
        description: "重启应用"
        when_to_use: "部署后重新拉起服务"
        exec_mode: async
        risk: "medium"
```

字段说明：

- `template`：命令模板，使用 `{0}`、`{1}` 等位置占位符
- `args`：参数名列表
- `examples`：参数示例，供 Agent 生成草案
- `risk`：`low`、`medium`、`high`
- `exec_mode`：`sync` 或 `async`

当使用分组结构时，完整命令键格式如下：

- `windows_common.<template_key>`
- `linux_common.<template_key>`
- `device_specific.<device_name>.<template_key>`

适用性约束：

- `windows_common.*` 仅允许在 `os_family=windows` 设备执行
- `linux_common.*` 仅允许在 `os_family=linux` 设备执行
- `device_specific.*` 仅允许在对应设备执行

### 配置不可用时的行为

- 服务不会退出
- 若配置文件不存在，会自动生成空模板文件
- 在配置恢复可用前，业务工具调用会返回 `CONFIG_UNAVAILABLE`
- 后台监测线程会自动重新加载修复后的配置

## 打包与发布

构建独立 exe：

```bat
build-exe.bat --clean
build-exe.bat --onedir
```

构建 wheel 和 sdist：

```bat
release.bat
```

仓库中的 `build/`、`dist/` 和 `src/*.egg-info/` 都属于生成物，不应作为长期版本化内容保留。

## 测试

当前仓库已包含基础 `unittest` 用例，可直接运行：

```powershell
python -m unittest discover -s tests
```

优先关注 `config.py`、`security.py`、`server.py` 的配置与安全边界测试。

## 常见问题

### 连接失败

优先检查网络可达性、SSH 端口、账号认证方式以及 `known_hosts` 配置。

### 模板不存在或不适用

先调用 `command_template_list` 和 `command_template_get`，确认完整 `command_key`、`os_family` 和设备归属。

### 文件操作被拒绝

检查目标路径是否位于 `allowed_roots` 白名单内。
