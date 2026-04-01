# mcp-device-gateway

面向嵌入式设备工作流的 MCP 服务。它通过设备清单、命令模板、路径白名单和审计日志，为 Agent 提供受控的 SSH/SFTP 访问能力；默认优先使用命令模板，在必要时也允许执行同样受安全规则约束的自定义拼装命令。

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
- 命令执行：按命令模板执行同步命令、批量并发命令、异步长任务，并支持受安全规则约束的自定义拼装命令
- 文件操作：上传、下载、列目录、查询远端文件元信息
- 配置辅助：配置摘要资源、任务推荐、设备操作提示模板
- 安全约束：参数正则校验、远端路径白名单/黑名单、内置危险命令拦截、结构化审计日志

当前服务暴露 17 个 MCP 工具、1 个资源和 1 个提示模板。

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

- 优先执行 `command_templates` 中声明的模板；模板未覆盖时可在 `cmd_exec` 中传入 `command` 执行受同等安全规则约束的自定义命令
- 所有 cmd_exec 参数都通过 `^[a-zA-Z0-9_./:=+\-]+$` 的 fullmatch 校验
- 所有远端文件路径都经过 PurePosixPath 规范化并受 allowed_roots 约束
- 可通过 denied_paths 为单设备显式封禁目录/文件（目录前缀命中即拒绝）
- 所有远端文件路径先做 denied_paths 检查，再做 allowed_roots 检查
- 内置拦截 Linux/Windows 明确高危命令（如 rm -rf /、mkfs、diskpart、format、强制递归删除系统盘路径）
- Linux 内核模块加载/卸载默认仍被拦截；仅当设备显式开启 `allow_kernel_module_ops: true` 时，才允许用于驱动开发验证调试
- 内置敏感路径（/etc/shadow、/root/.ssh、SAM hive 等）对所有设备文件操作始终拦截，不可被配置覆盖
- 配置了 known_hosts 时使用 RejectPolicy，未配置时才退化为 AutoAddPolicy
- 每次工具调用都写入 JSONL 审计日志，字段包括时间、工具名和关键载荷

## 工具总览

### 发现与规划

- `device_list`：列出设备清单
- `device_profile_get`：查看设备画像、能力标签和推荐模板（不暴露连接地址和安全策略细节）
- `device_ping`：验证 SSH 连通性
- `command_template_list`：列出全部模板及用途、参数、风险、执行模式
- `command_template_get`：查看单个模板详情
- `capability_overview`：返回能力地图和推荐工作流
- `task_recommend`：根据自然语言任务生成调用草案

### 命令执行

- `cmd_exec`：统一执行入口，支持单条/多条命令；每条可独立设置 `mode=sync/async`
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
4. 执行命令统一使用 `cmd_exec`：单条传 `command_key`（模板）或 `command`（自定义）；多条传 `commands` 列表。
5. 每条命令按场景设置 `mode=sync/async`；异步条目提交后调用 `cmd_exec_result` 轮询。
6. 模板暂未覆盖目标场景时，可在 `cmd_exec` 中改用 `command` 自定义命令，仍会经过危险命令与敏感路径检查。
7. 文件场景使用 `dir_list`、`file_stat`、`file_upload`、`file_download`。

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
- `MCP_LOCAL_ALLOWED_ROOTS`：本地上传/下载白名单根目录，多个路径使用系统 `PATH` 分隔符拼接；未设置时保持兼容，不限制本地路径
- `MCP_ASYNC_JOB_TTL_SEC`：已完成异步任务结果的保留秒数，默认 1800 秒；过期后 `cmd_exec_result` 会返回 `JOB_EXPIRED`
- `MCP_MAX_CUSTOM_COMMAND_LEN`：自定义命令最大长度，默认 4096；超过后返回 `INVALID_CUSTOM_COMMAND`

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
- `port` 必须是 `1..65535` 的整数，默认 `22`
- `allowed_roots: []` 表示不限制远端路径，只建议在可信开发环境使用
- `denied_paths` 可声明禁止访问的目录或文件，优先级高于 `allowed_roots`
- `description`、`when_to_use`、`capabilities`、`tags`、`preferred_templates` 用于提升 Agent 选型准确度
- `os_family` 仅支持 `linux` 或 `windows`，默认 `linux`
- `allow_kernel_module_ops` 默认为 `false`；仅建议在驱动开发验证调试设备上显式设为 `true`

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

### alias_registry

- `alias_registry` 用于把常用短名称映射到完整模板键，例如 `quick_status -> linux_common.health_check`
- 启动时会校验 alias 是否指向已存在模板
- alias 不允许与已有完整模板键或 `short_key` 冲突，避免运行时出现歧义

### 内置危险命令拦截

命令模板渲染后，或自定义命令提交前，都会按设备 `os_family` 进行危险命令检测，命中后返回 `DANGEROUS_COMMAND_BLOCKED`，命令不会下发到设备。命令执行统一通过 `cmd_exec`，均适用此拦截逻辑。

#### Linux 拦截项

| 类别 | 拦截规则 |
| ---- | -------- |
| 文件系统 | `rm -rf /`、`mkfs*`、`fdisk`、`parted`、`dd of=/dev/*` |
| 权限 | `chmod 777 /`（数字/符号模式）、`chown -R ... /` |
| 网络安全 | `iptables -F/X`、`nft flush ruleset`、`tcpdump -w` |
| 系统控制 | `shutdown/reboot/poweroff/halt`、`fork bomb`、`sysctl -w`、`echo > /proc/sysrq-trigger`、`kill 1` |
| 驱动调试（条件放开） | `insmod/rmmod/modprobe` 默认拦截；仅当设备显式设置 `allow_kernel_module_ops: true` 时允许 |
| 账户管理 | `useradd/userdel/usermod`、`groupadd/groupdel/groupmod`、`passwd`、`visudo` |
| 持久化 | `crontab -e/-r`、管道写入 crontab、`at` 定时任务 |
| 代码注入 | `curl/wget \| bash`、`base64 -d \| bash`、`python/perl/ruby/lua -c/-e` 内联代码 |
| 反弹 Shell | `bash /dev/tcp/...` TCP 重定向、`nc -e /bin/sh`、`socat exec:` |
| 进程渗透 | `strace/ltrace -p`、`gdb -p`（进程附加内存读取） |
| 容器逃逸 | `nsenter`、`unshare` |
| 内存读取 | `dd if=/dev/mem`、`cat /proc/kcore` |
| 内核注入 | `LD_PRELOAD=` 动态库注入 |
| 嵌入式设备 | `flash_erase/nandwrite`、`ubiformat` 等 MTD/UBI 操作、`fw_setenv` U-Boot 环境变量修改 |

#### Windows 拦截项

| 类别 | 拦截规则 |
| ---- | -------- |
| 磁盘 | `format <盘符>:`、`diskpart`、`bcdedit` |
| 删除 | `Remove-Item -Recurse -Force <盘符>:/`、`del /f /s /q <盘符>:/` |
| 系统控制 | `shutdown`、`Restart-Computer` |
| 账户与注册表 | `net user`、`net localgroup .../add`、`reg delete/add/import/export` |
| 防火墙 | `netsh firewall/advfirewall ... disable/reset/delete` |
| 代码注入 | `Invoke-Expression/iex`、`-EncodedCommand`、`Invoke-WebRequest \| iex` |
| 权限 | `icacls/cacls/takeown` 操作 Windows 系统目录 |
| 证据清除 | `vssadmin delete Shadows`、`wmic shadowcopy delete`（勒索软件）、`wevtutil cl` 事件日志清除 |
| 执行策略绕过 | `Set-ExecutionPolicy Bypass/Unrestricted` |
| 持久化 | `schtasks /create`、`sc create/config/delete` |
| LOL-bin | `mshta`、`rundll32`、`regsvr32`（squiblydoo）、`wscript/cscript`、`certutil -urlcache` |
| AV 禁用 | `Set-MpPreference -Disable*`、`Add-MpPreference -Exclusion*` |

### 内置敏感路径拦截

文件类操作（`dir_list`、`file_stat`、`file_upload`、`file_download`）和命令执行（`cmd_exec`）均会检查内置敏感路径。命中后返回 `SENSITIVE_PATH_BLOCKED`，优先级高于 `denied_paths` 和 `allowed_roots` 配置。

文件类工具还要求 `remote_path` 必须是绝对路径：Linux 使用 `/...`，Windows 使用 `C:/...` 形式；相对路径会直接返回 `REMOTE_PATH_MUST_BE_ABSOLUTE`。在进入白名单、黑名单和敏感路径判断前，路径还会先做规范化并消解 `.` / `..`，防止目录穿越绕过校验。

#### Linux 内置敏感路径

| 路径 | 说明 |
| ---- | ---- |
| `/etc/shadow`、`/etc/gshadow` | 系统/用户组密码哈希 |
| `/etc/sudoers`、`/etc/sudoers.d` | sudo 权限配置 |
| `/root/.ssh`、`/root/.gnupg` | root SSH/GPG 私钥 |
| `/etc/ssl/private`、`/etc/pki` | TLS/SSL 证书私钥 |
| `/etc/passwd`、`/etc/hosts` | 账户信息/本地 DNS 解析 |
| `/etc/pam.d`、`/etc/security` | PAM 认证配置（修改可绕过身份验证） |
| `/etc/ld.so.preload`、`/etc/ld.so.conf` | 动态链接器配置（库注入向量） |
| `/etc/crontab`、`/etc/cron.d`、`/var/spool/cron` | 定时任务（持久化手法） |
| `/etc/profile.d`、`/etc/init.d`、`/etc/systemd/system`、`/etc/rc.local` | Shell/服务启动脚本（持久化手法） |
| `/etc/ssh/sshd_config` | SSH 服务端配置 |
| `/proc/1/environ` | init 进程环境变量（常含明文凭据） |
| `/boot` | 引导加载程序与内核镜像（变砖风险） |
| `/dev/mem`、`/dev/kmem`、`/proc/kcore` | 物理/内核内存（凭据泄漏） |
| `/dev/mtd` | 嵌入式 MTD Flash 设备节点（写入可永久损坏设备） |

#### Windows 内置敏感路径

| 路径 | 说明 |
| ---- | ---- |
| `C:/Windows/System32/config` | SAM/SECURITY/SYSTEM 注册表 hive（凭据核心） |
| `C:/Windows/NTDS` | Active Directory 数据库 |
| `C:/Users/All Users/Microsoft/Crypto` | 系统密钥材料 |
| `C:/Windows/System32/drivers/etc/hosts` | hosts 文件（DNS 劫持） |
| `C:/ProgramData/Microsoft/Windows/Start Menu/Programs/Startup` | 自启动目录（持久化） |
| `C:/Windows/System32/Tasks` | 计划任务 XML 文件（持久化） |
| `C:/Windows/System32/winevt/Logs` | 事件日志（防止日志篡改/删除） |

当使用分组结构时，完整命令键格式如下：

- `windows_common.<template_key>`
- `linux_common.<template_key>`
- `device_specific.<device_name>.<template_key>`

命令执行工具（`cmd_exec`）对 `command_key` 的解析规则：

- 默认 `strict=false`：优先按完整键精确匹配；未命中时允许按 `short_key` 自动解析（先设备作用域，再全局）。
- `strict=true`：仅接受完整键，不做短键容错。
- 解析失败时会返回候选提示，例如 `Did you mean: linux_common.health_check`。

`cmd_exec` 在多条模式（`commands`）下额外支持：

- `fail_fast=false`（默认）：单条失败不影响其他命令，返回逐条结果（含每条 `status` / `error`）。
- `fail_fast=true`：遇到第一条校验错误立即失败，行为与历史版本一致。

参数校验新增强类型模式（兼容旧模式）：

- 若模板配置 `arg_schema`，则按类型校验参数（`path`、`int`、`enum`、`bool`、`service_name`、`filename`）。
- `path` 类型支持 UTF-8 与空格（可通过 `allow_utf8` / `allow_space` 控制），并拒绝命令拼接危险字符。
- `path` 类型参数在包含空格时会自动按设备 OS 做安全加引号；若模板自身已经写了包裹 `{0}` 的引号，则不会重复加引号。
- 未配置 `arg_schema` 时，仍沿用原有 `args_sanitize_pattern` 规则，保持兼容。
- Windows 路径会统一按 `/` 形式并以大小写无关方式规范化，再参与 `allowed_roots`、`denied_paths` 与内置敏感路径校验。

执行结果增强：

- 返回 `normalized_status`（`success|partial|failed`）。
- 模板可配置 `success_on_exit_codes`（例如 `[0,124]`）。
- 返回 `collected_duration_ms` 与 `output_truncated`，用于区分“超时但有有效输出”的场景。
- 模板可声明 `parser`，返回 `structured_output`（并保留 `raw_output`）。

统一响应外层（兼容模式）：

- `cmd_exec`、`cmd_exec_result`、`command_template_list`、`command_template_get` 支持 `compat_mode`。
- 默认 `compat_mode=legacy`：返回历史结构，保证旧客户端兼容。
- `compat_mode=v1_1`：返回统一外层字段：`request_id`、`timestamp`、`api_version`、`status`、`error`、`data`、`meta`。
- 在 `compat_mode=v1_1` 下，常见业务错误会返回结构化 `error` 对象（`code/message/details/recoverable/suggestion`），而不是直接抛异常。
- 在 `compat_mode=legacy` 下，仍保持历史抛异常行为。

稳定错误码（兼容保留旧错误语义）：

- `GW-1002`：参数错误（对应 `INVALID_COMMAND_ARGS`）。
- `GW-1004`：分页参数非法（对应 `INVALID_PAGE`）。
- `GW-1005`：分页大小非法（对应 `INVALID_PAGE_SIZE`）。
- `GW-2001`：模板未找到（对应 `UNKNOWN_COMMAND_TEMPLATE`）。
- `GW-2003`：模板不适用于当前设备（对应 `TEMPLATE_NOT_APPLICABLE`）。
- `GW-2004`：模板声明执行模式与请求模式不匹配（对应 `EXEC_MODE_MISMATCH`）。
- `GW-3001`：设备未找到（对应 `DEVICE_NOT_FOUND/UNKNOWN_DEVICE`）。
- `GW-4002`：异步任务未找到（对应 `UNKNOWN_JOB`）。

模板列表接口增强：

- `command_template_list` 支持 `device_name`、`category`、`exec_mode`、`keyword` 过滤。
- 支持 `page/page_size` 分页与 `only_fields` 字段裁剪。
- `page` 必须 `>= 1`，`page_size` 取值范围为 `1..500`；非法值在 `v1_1` 下会返回结构化错误对象。
- 支持 `changed_since` 增量同步。

权限提示增强：

- 模板可声明 `requires_privilege`（`none|sudo|root`）、`fallback_templates` 与 `capability_tags`。
- 权限失败时，响应会给出 `permission_suggestion` 与可替代模板建议。

别名与纠错增强：

- 顶层配置支持 `alias_registry`（常用名映射到完整模板 key）。
- `task_recommend` 可对 full key / short key / alias 做自动纠正并返回精确模板键。

适用性约束：

- `windows_common.*` 仅允许在 `os_family=windows` 设备执行
- `linux_common.*` 仅允许在 `os_family=linux` 设备执行
- `device_specific.*` 仅允许在对应设备执行
- `cmd_exec` 会校验模板 `exec_mode` 与请求 `mode` 一致（模板 sync 走同步，模板 async 走异步）
- `file_upload` / `file_download` 在配置了 `MCP_LOCAL_ALLOWED_ROOTS` 时，只允许读写命中的本地根目录

### 配置不可用时的行为

- 服务不会退出
- 若配置文件不存在，会自动生成空模板文件
- 在配置恢复可用前，业务工具调用会返回 `CONFIG_UNAVAILABLE`
- 后台监测线程会自动重新加载修复后的配置
- 配置热加载成功后会主动关闭 SSH 连接池，避免旧连接继续复用旧凭据或旧 `known_hosts` 策略

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

## 许可证

本项目采用 MIT 许可证发布。完整条款见 [LICENSE](LICENSE)。
