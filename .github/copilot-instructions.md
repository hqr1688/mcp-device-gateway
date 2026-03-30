# 项目指南 — mcp-device-gateway

面向嵌入式设备工作流的 MCP（Model Context Protocol）服务器。通过路径白名单、命令模板和审计日志，为 AI Agent 提供受控的 SSH/SFTP 访问能力。

用户文档详见 [README.md](../README.md)。

## 文档语言规范

**本项目所有文档均使用中文**，包括：

- Markdown 文档（README、CHANGELOG、设计文档等）
- 代码注释与 docstring
- 提交信息（commit message）
- 配置文件中的说明性注释
- 所有语言都使用中文

## 构建与运行

```powershell
# 安装（可编辑模式）
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .

# 启动服务
mcp-device-gateway
# 或
python -m mcp_device_gateway.server

# 暂无测试套件 — security.py 和 config.py 是优先级最高的测试目标
```

**环境变量**（均为可选，覆盖默认值）：

| 变量                  | 默认值                     | 用途                       |
| --------------------- | -------------------------- | -------------------------- |
| `MCP_DEVICE_CONFIG` | `./devices.example.yaml` | 设备配置文件路径           |
| `MCP_AUDIT_LOG`     | `./mcp_audit.log`        | 审计日志路径（JSONL 格式） |
| `MCP_TRANSPORT`     | `stdio`                  | MCP 传输层                 |

## 架构

```
server.py          ← FastMCP 入口；定义 5 个工具；写入 JSONL 审计日志
├── config.py      ← 加载 YAML → 冻结数据类（AppConfig、DeviceConfig）
├── security.py    ← sanitize_args() 正则校验 + is_path_allowed() 前缀检查
└── ssh_client.py  ← SshDeviceClient 上下文管理器（paramiko）；ExecResult 数据类
```

**请求流程**：工具调用 → `_get_device()` → 安全检查 → `SshDeviceClient` → 执行 → 返回 + `_audit()`

**5 个 MCP 工具**：`device_list`、`device_ping`、`cmd_exec`、`file_upload`、`file_download`

## 代码约定

- **不可变配置**：所有配置/结果数据类使用 `@dataclass(frozen=True)`
- **类型注解**：每个模块均有 `from __future__ import annotations`，所有函数完整标注类型
- **路径处理**：设备端路径统一使用 `PurePosixPath`（跨平台 POSIX 规范化）
- **日志格式**：JSONL 格式，`ensure_ascii=False`；每行一个 JSON 对象
- **文件编码**：统一使用 `encoding="utf-8", errors="replace"`

## 安全关键模式

本项目的核心价值在于安全隔离——禁止削弱以下机制：

- **参数校验**（`security.py`）：对所有 `cmd_exec` 参数使用正则 `^[a-zA-Z0-9_./:\=+\-]+$` 的 `fullmatch()` 校验；不匹配则抛出 `ValueError`
- **路径白名单**（`security.py`）：`is_path_allowed()` 使用 `PurePosixPath` 前缀匹配 `allowed_roots`；空列表 = 无限制（依赖此行为时须在代码注释中明确说明）
- **命令模板**（`devices.yaml`）：使用 `{0}`、`{1}` 位置占位符；禁止在工具代码中拼接 shell 字符串
- **SSH 主机验证**：优先在设备配置中使用 `known_hosts`；仅在必要时使用 `AutoAddPolicy` 并附注释说明原因

新增或修改工具时，SSH 操作前必须调用 `sanitize_args()` / `is_path_allowed()`，操作后必须调用 `_audit()`。

## 配置文件格式

规范参考见 [devices.example.yaml](../devices.example.yaml)，关键规则：

- 每个设备必须有 `host` 和 `username`
- `allowed_roots: []` 表示**不限制路径**（代码中依赖此行为时须明确注释）
- 命令模板参数使用 `.format()` 位置语法：`{0}`、`{1}`
