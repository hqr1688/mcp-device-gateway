# MCP 能力总览（mcp-device-gateway）

## 1. 服务目标与边界

### 1.1 目标

本服务为 Agent 提供受控的远端设备操作能力，核心目标是：

- 在白名单边界内执行设备命令与文件传输
- 通过命令模板与参数校验降低注入风险
- 通过审计日志实现调用可追溯
- 让 Agent 能“先发现能力，再安全执行”

### 1.2 边界

- 不提供任意 shell 能力，仅能执行 `command_templates` 中定义的模板
- 文件读写受 `allowed_roots` 白名单约束
- 参数默认按正则 `^[a-zA-Z0-9_./:\=+\-]+$` 校验
- 配置不可用时服务不退出，但拒绝业务工具调用

## 2. 工具能力与使用方式

### 2.1 发现类工具（推荐先用）

1. `capability_overview`
用途：获取能力地图、工具用途、推荐流程。
何时用：首次接入、任务中途不确定下一步。

2. `command_template_list`
用途：列出所有命令模板的描述、参数、示例、风险。
何时用：不确定 `command_key` 时。

3. `command_template_get`
用途：查看单个模板详细信息。
何时用：已锁定 `command_key`，需要确认参数与示例。

4. `task_recommend`
用途：输入自然语言任务，返回推荐 `tool + command_key + 参数草案`。
何时用：知道目标但不清楚工具调用细节。

5. `device_profile_get`
用途：查看指定设备画像（用途、能力标签、推荐模板）。
何时用：存在多个设备，需要先选最合适目标时。

### 2.2 执行类工具

1. `device_list`
用途：列出设备，作为后续调用的设备来源。

2. `device_ping`
用途：验证设备连通性。

3. `cmd_exec`
用途：按模板执行命令。
前置：建议先 `command_template_list/get`。

4. `file_upload`
用途：上传本地文件到设备。

5. `file_download`
用途：下载设备文件到本地。

## 3. 推荐工作流

1. 调用 `capability_overview` 获取当前能力地图
1. 调用 `device_list` 选择目标设备
1. 调用 `device_ping` 验证可达性
1. 命令执行场景：

- 优先 `task_recommend` 或 `command_template_list`
- 必要时 `command_template_get`
- 最后 `cmd_exec`

1. 文件传输场景：

- `file_upload` 或 `file_download`

## 4. 常见错误码与处理建议

1. `CONFIG_UNAVAILABLE`
含义：配置文件不可用或格式错误。
建议：修复配置文件并保存，等待自动重载。

2. `UNKNOWN_DEVICE`
含义：设备名不存在。
建议：先调用 `device_list` 获取有效设备名。

3. `UNKNOWN_COMMAND_TEMPLATE`
含义：命令模板不存在。
建议：先调用 `command_template_list` 或 `command_template_get`。

4. `INVALID_COMMAND_ARGS`
含义：传入参数与模板占位符不匹配。
建议：检查模板 `arg_count` / `args` / `examples`。

5. `LOCAL_FILE_NOT_FOUND`
含义：上传时本地文件不存在。
建议：确认路径与文件权限。

6. `REMOTE_PATH_NOT_ALLOWED`
含义：远端路径不在白名单中。
建议：调整到 `allowed_roots` 内路径，或更新配置。

## 5. 模板编写规范（结构化 command_templates）

支持两种写法：

1. 简写：

```yaml
command_templates:
  health_check: "uname -a; uptime"
```

1. 结构化（推荐）：

```yaml
command_templates:
  list_dir:
    template: "ls -al {0}"
    description: "列出指定目录"
    when_to_use: "需要检查目录内容"
    args: ["path"]
    examples:
      - ["/opt/idm"]
      - ["/tmp"]
    risk: "low"
```

字段说明：

- `template`：命令模板，支持 `{0}`、`{1}` 占位符
- `description`：简短功能说明
- `when_to_use`：推荐使用场景
- `args`：参数名列表（按位置）
- `examples`：参数示例二维数组，供 Agent 生成参数草案
- `risk`：`low`/`medium`/`high`

建议：

1. 每个模板至少提供 `description` 与 `when_to_use`
2. 含占位符模板必须给 `args`，并提供至少一个 `examples`
3. 高风险命令（重启、删除、覆盖）明确标记 `risk: high`

## 6. 设备画像编写规范（devices）

建议为每个设备补齐以下字段，提升 Agent 选型准确率：

- `description`：一句话说明设备职责
- `when_to_use`：典型任务场景
- `capabilities`：能力标签列表（如 `shell-command`、`file-transfer`）
- `tags`：环境或平台标签（如 `dev`、`prod`、`arm64`）
- `preferred_templates`：该设备优先使用的命令模板 key
- `os_family`：系统家族（`linux`/`windows`，默认 `linux`）
- `os_name`：系统名称（如 Ubuntu、Windows Server）
- `os_version`：系统版本（如 22.04、2019）

示例：

```yaml
devices:
  devkit-01:
    host: 192.168.200.218
    username: root
    os_family: "linux"
    os_name: "Ubuntu"
    os_version: "22.04"
    description: "开发验证板卡"
    when_to_use: "联调与日志排障"
    capabilities: ["shell-command", "file-transfer", "log-inspection"]
    tags: ["dev", "arm64"]
    preferred_templates: ["linux_common.health_check", "device_specific.devkit-01.idm_status"]
```
