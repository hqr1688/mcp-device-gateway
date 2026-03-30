# MCP 1 分钟回归自检清单

用于每次修改配置、重启服务或更新代码后，快速确认 `embedded-device-gateway` 可用。

## 0. 前置条件

- VS Code 已重载窗口，Copilot Chat 已加载 MCP 服务。
- 设备配置文件可用，且 `device_ping` 能通过。

## 1. 基础可用性（10 秒）

在 Copilot Chat 中依次调用：

1. `device_list`
2. `device_profile_get(device_name="devkit-01")`
3. `device_ping(device_name="devkit-01")`

通过标准：

- `device_list` 返回 `devkit-01`
- `device_profile_get` 返回 `capabilities/tags/preferred_templates`
- `device_ping` 返回 `reachable=true`

## 2. 能力发现（10 秒）

调用：

1. `capability_overview()`
2. `command_template_list()`
3. `command_template_get(command_key="linux_common.health_check")`
4. `task_recommend(task="查看 /opt/idm 目录内容")`

通过标准：

- `capability_overview` 返回 `workflow` 且包含 `task_recommend`
- `command_template_list` 返回模板分类字段（`category`）
- `command_template_get` 返回 `examples`
- `task_recommend` 返回 `recommended_tool` 与 `draft`

## 3. 命令执行（15 秒）

调用：

1. `cmd_exec(device_name="devkit-01", command_key="linux_common.health_check")`
2. `cmd_exec(device_name="devkit-01", command_key="device_specific.devkit-01.list_idm_dir")`
3. `cmd_exec(device_name="devkit-01", command_key="device_specific.devkit-01.idm_log_list")`

通过标准：

- 两次调用都 `exit_code=0`
- `stderr` 为空或仅少量非致命提示

## 4. 上传下载闭环（25 秒）

先准备本地文件，例如：

- 本地路径：`D:/prj/mcp-device-gateway/mcp_transfer_test.txt`

在 Copilot Chat 中调用：

1. `file_upload(device_name="devkit-01", local_path="D:/prj/mcp-device-gateway/mcp_transfer_test.txt", remote_path="/tmp/mcp_transfer_test.txt")`
2. `file_download(device_name="devkit-01", remote_path="/tmp/mcp_transfer_test.txt", local_path="D:/prj/mcp-device-gateway/mcp_transfer_test_downloaded.txt")`

通过标准：

- 两次调用都返回 `ok=true`
- 下载文件内容与源文件一致（或哈希一致）

## 5. 安全策略验证（10 秒）

调用一个越界路径（示例）：

1. `file_download(device_name="devkit-01", remote_path="/etc/passwd", local_path="D:/prj/mcp-device-gateway/forbidden.txt")`

通过标准：

- 返回路径不允许错误（`Remote path is not allowed`）

再调用一个设备专属模板越权示例：

1. 选择一个在配置中**真实存在**、但属于其他设备的 `device_specific` 模板 key。
2. 用当前设备调用它（示例）：
	`cmd_exec(device_name="devkit-01", command_key="device_specific.devkit-02.service_status", args=["app"])`

通过标准：

- 返回模板适用范围错误（`TEMPLATE_NOT_APPLICABLE` 或设备不匹配）

## 6. 快速结论模板

- 基础连通：通过/失败
- 能力发现：通过/失败
- 命令执行：通过/失败
- 上传下载：通过/失败
- 安全策略：通过/失败
- 备注：异常信息摘要（如有）
