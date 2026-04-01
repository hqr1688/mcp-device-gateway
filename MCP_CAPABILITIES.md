# MCP 能力总览（mcp-device-gateway）

## 服务边界

- 优先执行配置中声明的命令模板；模板未覆盖时可执行同样受安全规则约束的自定义拼装命令
- 所有命令参数都通过 `^[a-zA-Z0-9_./:=+\-]+$` 正则校验
- 支持模板 `arg_schema` 强类型参数校验（path/int/enum/bool/service_name/filename）
- 远端文件操作受 `allowed_roots` 白名单约束
- 配置不可用时服务保持运行，但拒绝业务工具调用
- 每次调用都会写入 JSONL 审计日志

## 能力清单

当前服务提供：

- 17 个 MCP 工具
- 1 个资源：`config://summary`
- 1 个提示模板：`device_ops_prompt(task, device_name?)`

### 发现与规划工具

1. `device_list`
用途：列出全部设备。
何时用：任何设备操作前的第一步。

2. `device_profile_get`
用途：查看设备画像、能力标签、推荐模板。
补充：默认不暴露连接地址、账号、路径白名单/黑名单等敏感配置细节。
何时用：需要从多个设备中选目标时。

3. `device_ping`
用途：验证设备 SSH 可达性。
何时用：执行命令或文件传输前。

4. `command_template_list`
用途：列出全部命令模板、参数、风险和执行模式。
何时用：不确定 `command_key` 时。
补充：支持按 `device_name/category/exec_mode/keyword` 过滤，支持 `page/page_size` 分页、`only_fields` 字段裁剪、`changed_since` 增量同步。

5. `command_template_get`
用途：查看单个模板详情。
何时用：已锁定模板，需确认 `exec_mode`、参数和风险时。

6. `capability_overview`
用途：返回能力地图与推荐工作流。
何时用：首次接入或执行过程中不确定下一步时。

7. `task_recommend`
用途：根据自然语言任务返回推荐工具与参数草案。
何时用：知道想做什么，但不确定要调哪些工具时。
补充：默认优先推荐命令模板；当模板不命中时，会回退推荐 `custom_exec` 或 `custom_exec_async`（仍受同一套安全拦截规则约束）。

### 命令执行工具

1. `cmd_exec`
用途：执行单条同步命令。
何时用：模板 `exec_mode=sync`，并且需要立即拿到结果时。
补充：默认 `strict=false`，支持 `short_key` 自动解析（先设备作用域，再全局）；`strict=true` 仅接受完整键。
补充：返回 `normalized_status`、`success_on_exit_codes`、`collected_duration_ms`、`structured_output`、`raw_output`。

2. `cmd_exec_batch`
用途：并发执行多条独立同步命令。
何时用：一次采集多项指标或多条互不依赖的短命令时。
补充：支持 `fail_fast`。默认 `fail_fast=false`，单条失败不会中断整批，会按输入顺序返回逐条结果。

3. `cmd_exec_async`
用途：提交长耗时异步命令，立即返回 `job_id`。
何时用：模板 `exec_mode=async` 时。
补充：最终结果同样包含 `normalized_status` 与结构化输出字段。

4. `cmd_exec_result`
用途：查询异步任务执行状态和结果。
何时用：调用 `cmd_exec_async` 或 `custom_exec_async` 之后轮询结果时。

5. `custom_exec`
用途：执行符合安全规则的自定义拼装命令。
何时用：现有模板未覆盖目标场景，但命令本身仍可通过危险命令与敏感路径检查时。

6. `custom_exec_async`
用途：异步提交符合安全规则的自定义拼装命令。
何时用：现有模板未覆盖目标场景，且命令执行耗时较长时。

### 文件与目录工具

1. `dir_list`
用途：列出远端目录内容。
何时用：确认目录结构、文件是否存在、文件大小时。

2. `file_stat`
用途：查看远端文件或目录元信息。
何时用：下载前确认目标是否存在以及大小、时间等属性。

3. `file_upload`
用途：上传本地文件到设备。
何时用：下发构建产物、脚本或配置时。

4. `file_download`
用途：从设备下载文件到本地。
何时用：回收日志、导出配置或拉取调试产物时。

## 推荐工作流

1. 调用 `device_list` 选择目标设备。
2. 必要时调用 `device_profile_get`，确认该设备是否适合当前任务。
3. 调用 `device_ping` 验证连通性。
4. 命令类任务先调用 `task_recommend` 或 `command_template_list`。
5. 使用 `command_template_get` 查看模板详情，重点确认 `exec_mode`。
6. `exec_mode=sync` 时用 `cmd_exec`，多条短命令用 `cmd_exec_batch`。
7. `exec_mode=async` 时用 `cmd_exec_async`，然后用 `cmd_exec_result` 轮询。
8. 模板未覆盖时，可改用 `custom_exec` 或 `custom_exec_async`。
9. 文件类任务使用 `dir_list`、`file_stat`、`file_upload`、`file_download`。

## 常见错误码

1. `CONFIG_UNAVAILABLE`
含义：配置文件不存在、格式错误或当前不可加载。
建议：修复配置文件并保存，等待自动重载。

2. `UNKNOWN_DEVICE`
含义：设备名不存在。
建议：先调用 `device_list` 获取有效设备名。

3. `UNKNOWN_COMMAND_TEMPLATE`
含义：命令模板不存在。
建议：先调用 `command_template_list` 或 `command_template_get`；失败响应会携带候选提示（`Did you mean`）。
错误码：`GW-2001`。

4. `TEMPLATE_NOT_APPLICABLE`
含义：模板与设备或 `os_family` 不匹配。
建议：检查模板分组、设备归属和 `os_family`。

5. `INVALID_COMMAND_ARGS`
含义：传入参数与模板占位符数量不匹配，或参数不满足安全校验。
建议：检查错误中的 `expected_args`、`received_args`、`examples` 与 `template_summary` 字段。
错误码：`GW-1002`。

6. `DEVICE_NOT_FOUND`
含义：设备名不存在。
建议：先调用 `device_list` 获取有效设备名。
错误码：`GW-3001`。

7. `UNKNOWN_JOB`
含义：异步任务 ID 不存在。
建议：确认 `job_id` 是否来自当前服务实例。
错误码：`GW-4002`。

## v1.1 响应外层

以下工具支持 `compat_mode=v1_1`：

- `command_template_list`
- `cmd_exec`
- `cmd_exec_batch`
- `cmd_exec_async`
- `cmd_exec_result`
- `custom_exec`
- `custom_exec_async`

开启后统一返回：

- `request_id`
- `timestamp`
- `api_version`
- `status`
- `error`
- `data`
- `meta`

失败时 `error` 字段结构：

- `code`：稳定错误码（如 `GW-2001`）
- `message`：人类可读信息
- `details`：结构化细节（包含 `machine_code` 与原始错误）
- `recoverable`：是否建议重试/修复后重试
- `suggestion`：修复建议（如候选模板）

默认 `compat_mode=legacy`，保持历史返回结构，避免破坏旧客户端。

1. `LOCAL_FILE_NOT_FOUND`
含义：上传时本地文件不存在。
建议：确认本地路径和文件权限。

2. `REMOTE_PATH_NOT_ALLOWED`
含义：远端路径不在白名单内。
建议：改用 `allowed_roots` 范围内路径，或更新配置。

3. `INVALID_CUSTOM_COMMAND`
含义：自定义命令为空、包含换行，或不满足单行约束。
建议：将命令整理为单行，再通过 `custom_exec` 或 `custom_exec_async` 提交。

## 配置建议

### devices

- 每个设备都应提供 `host` 和 `username`
- 建议补齐 `description`、`when_to_use`、`capabilities`、`tags`、`preferred_templates`
- 生产环境优先使用 `key_file + known_hosts`，避免明文密码和弱主机校验
- `allowed_roots` 尽量最小化，空列表仅用于可信环境

### command_templates

- 推荐使用分组结构：`windows_common`、`linux_common`、`device_specific`
- 含占位符的模板应补齐 `args` 和 `examples`
- 建议补齐 `arg_schema`，明确参数类型与边界（尤其是路径类参数）
- 高风险操作明确标记 `risk`
- 秒级命令使用 `exec_mode: sync`
- 编译、部署、重启等长任务使用 `exec_mode: async`
- 可选配置 `requires_privilege`、`fallback_templates`、`capability_tags` 与 `parser`
- 可通过 `alias_registry` 统一常用命名，降低 short_key/命名空间心智负担
