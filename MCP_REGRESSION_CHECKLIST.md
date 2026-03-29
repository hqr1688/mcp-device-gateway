# MCP 1 分钟回归自检清单

用于每次修改配置、重启服务或更新代码后，快速确认 `embedded-device-gateway` 可用。

## 0. 前置条件

- VS Code 已重载窗口，Copilot Chat 已加载 MCP 服务。
- 设备配置文件可用，且 `device_ping` 能通过。

## 1. 基础可用性（10 秒）

在 Copilot Chat 中依次调用：

1. `device_list`
2. `device_ping(device_name="devkit-01")`

通过标准：

- `device_list` 返回 `devkit-01`
- `device_ping` 返回 `reachable=true`

## 2. 命令执行（15 秒）

调用：

1. `cmd_exec(device_name="devkit-01", command_key="health_check")`
2. `cmd_exec(device_name="devkit-01", command_key="list_idm_dir")`
3. `cmd_exec(device_name="devkit-01", command_key="idm_log_list")`

通过标准：

- 两次调用都 `exit_code=0`
- `stderr` 为空或仅少量非致命提示

## 3. 上传下载闭环（25 秒）

先准备本地文件，例如：

- 本地路径：`D:/prj/mcp-device-gateway/mcp_transfer_test.txt`

在 Copilot Chat 中调用：

1. `file_upload(device_name="devkit-01", local_path="D:/prj/mcp-device-gateway/mcp_transfer_test.txt", remote_path="/tmp/mcp_transfer_test.txt")`
2. `file_download(device_name="devkit-01", remote_path="/tmp/mcp_transfer_test.txt", local_path="D:/prj/mcp-device-gateway/mcp_transfer_test_downloaded.txt")`

通过标准：

- 两次调用都返回 `ok=true`
- 下载文件内容与源文件一致（或哈希一致）

## 4. 安全策略验证（10 秒）

调用一个越界路径（示例）：

1. `file_download(device_name="devkit-01", remote_path="/etc/passwd", local_path="D:/prj/mcp-device-gateway/forbidden.txt")`

通过标准：

- 返回路径不允许错误（`Remote path is not allowed`）

## 5. 快速结论模板

- 基础连通：通过/失败
- 命令执行：通过/失败
- 上传下载：通过/失败
- 安全策略：通过/失败
- 备注：异常信息摘要（如有）
