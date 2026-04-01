# MCP 能力总览（mcp-device-gateway）

## 服务边界

- 优先执行配置中声明的命令模板；模板未覆盖时可在 `cmd_exec` 中使用 `command` 自定义命令
- 所有命令参数都通过安全校验，模板参数支持 `arg_schema` 强类型校验（path/int/enum/bool/service_name/filename）
- 远端路径受 `allowed_roots` 白名单和 `denied_paths` 黑名单约束，并叠加内置敏感路径拦截
- 配置不可用时服务保持运行，但拒绝业务工具调用
- 每次调用都会写入 JSONL 审计日志

## 能力清单

当前服务提供：

- 13 个 MCP 工具
- 1 个资源：`config://summary`
- 1 个提示模板：`device_ops_prompt(task, device_name?)`

### 发现与规划工具

1. `device_list`：列出设备清单
2. `device_profile_get`：查看设备画像、能力标签、推荐模板
3. `device_ping`：验证设备 SSH 连通性
4. `command_template_list`：按条件列出模板（支持过滤、分页、字段裁剪）
5. `command_template_get`：查看单个模板详情
6. `capability_overview`：返回能力地图和推荐调用顺序
7. `task_recommend`：根据自然语言任务生成工具调用草案

### 命令执行工具

1. `cmd_exec`：统一执行入口
   - 支持单条：`command_key`（模板）或 `command`（自定义）
   - 支持多条：`commands` 列表
   - 每条命令可独立指定 `mode=sync/async`、`timeout_sec`、`strict`
   - 多条模式支持 `fail_fast`
2. `cmd_exec_result`：查询异步任务状态与结果
   - `running`：执行中
   - `done`：已完成，返回 `exit_code/stdout/stderr/elapsed_ms`
   - `error`：执行异常

### 文件与目录工具

1. `dir_list`：列出远端目录内容
2. `file_stat`：查看远端文件或目录元信息
3. `file_upload`：上传本地文件到设备
4. `file_download`：从设备下载文件到本地

## 推荐工作流

1. 调用 `device_list` 选择目标设备
2. 必要时调用 `device_profile_get` 确认设备能力
3. 调用 `device_ping` 验证连通性
4. 命令类任务先调用 `task_recommend` 或 `command_template_list`
5. 调用 `command_template_get` 确认参数、风险、执行模式
6. 调用 `cmd_exec` 执行（按条目选择 `mode=sync/async`）
7. 对异步条目调用 `cmd_exec_result` 轮询
8. 文件类任务使用 `dir_list`、`file_stat`、`file_upload`、`file_download`

## 常见错误码

1. `GW-1002`：参数错误（`INVALID_COMMAND_ARGS`）
2. `GW-1004`：分页参数非法（`INVALID_PAGE`）
3. `GW-1005`：分页大小非法（`INVALID_PAGE_SIZE`）
4. `GW-1006`：超时参数非法（`INVALID_TIMEOUT`）
5. `GW-1007`：自定义命令非法（`INVALID_CUSTOM_COMMAND`）
6. `GW-2001`：模板不存在（`UNKNOWN_COMMAND_TEMPLATE`）
7. `GW-2002`：模板短键歧义（`AMBIGUOUS_COMMAND_TEMPLATE`）
8. `GW-2003`：模板不适用于当前设备（`TEMPLATE_NOT_APPLICABLE`）
9. `GW-2004`：请求模式与模板模式不匹配（`EXEC_MODE_MISMATCH`）
10. `GW-3001`：设备不存在（`DEVICE_NOT_FOUND`）
11. `GW-4002`：异步任务不存在（`UNKNOWN_JOB`）
12. `GW-4003`：异步任务结果已过期（`JOB_EXPIRED`）

## v1.1 兼容响应外层

以下工具支持 `compat_mode=v1_1`：

- `command_template_list`
- `command_template_get`
- `cmd_exec`
- `cmd_exec_result`

开启后统一返回：`request_id`、`timestamp`、`api_version`、`status`、`error`、`data`、`meta`。

默认 `compat_mode=legacy`，保持历史结构兼容旧客户端。