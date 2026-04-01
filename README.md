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
- 安全约束：参数正则校验、远端路径白名单/黑名单、内置危险命令拦截、结构化审计日志

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
- 可通过 denied_paths 为单设备显式封禁目录/文件（目录前缀命中即拒绝）
- 所有远端文件路径先做 denied_paths 检查，再做 allowed_roots 检查
- 内置拦截 Linux/Windows 明确高危命令（如 rm -rf /、mkfs、diskpart、format、强制递归删除系统盘路径）
- 内置敏感路径（/etc/shadow、/root/.ssh、SAM hive 等）对所有设备文件操作始终拦截，不可被配置覆盖
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
- `denied_paths` 可声明禁止访问的目录或文件，优先级高于 `allowed_roots`
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

### 内置危险命令拦截

命令模板渲染后会按设备 `os_family` 进行危险命令检测，命中后返回 `DANGEROUS_COMMAND_BLOCKED`，命令不会下发到设备。`exec_*` 系列工具（`cmd_exec`、`cmd_exec_batch`、`cmd_exec_async`）均适用。

**Linux 拦截项**

| 类别 | 拦截规则 |
|------|---------|
| 文件系统 | `rm -rf /`、`mkfs*`、`fdisk`、`parted`、`dd of=/dev/*` |
| 权限 | `chmod 777 /`（数字/符号模式）、`chown -R ... /` |
| 网络安全 | `iptables -F/X`、`nft flush ruleset`、`tcpdump -w` |
| 系统控制 | `shutdown/reboot/poweroff/halt`、`insmod/rmmod/modprobe`、`fork bomb`、`sysctl -w`、`echo > /proc/sysrq-trigger`、`kill 1` |
| 账户管理 | `useradd/userdel/usermod`、`groupadd/groupdel/groupmod`、`passwd`、`visudo` |
| 持久化 | `crontab -e/-r`、管道写入 crontab、`at` 定时任务 |
| 代码注入 | `curl/wget \| bash`、`base64 -d \| bash`、`python/perl/ruby/lua -c/-e` 内联代码 |
| 反弹 Shell | `bash /dev/tcp/...` TCP 重定向、`nc -e /bin/sh`、`socat exec:` |
| 进程渗透 | `strace/ltrace -p`、`gdb -p`（进程附加内存读取） |
| 容器逃逸 | `nsenter`、`unshare` |
| 内存读取 | `dd if=/dev/mem`、`cat /proc/kcore` |
| 内核注入 | `LD_PRELOAD=` 动态库注入 |
| 嵌入式设备 | `flash_erase/nandwrite`、`ubiformat` 等 MTD/UBI 操作、`fw_setenv` U-Boot 环境变量修改 |

**Windows 拦截项**

| 类别 | 拦截规则 |
|------|---------|
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

文件类操作（`dir_list`、`file_stat`、`file_upload`、`file_download`）和命令执行（`cmd_exec`、`cmd_exec_batch`、`cmd_exec_async`）均会检查内置敏感路径。命中后返回 `SENSITIVE_PATH_BLOCKED`，优先级高于 `denied_paths` 和 `allowed_roots` 配置。

**Linux 内置敏感路径**

| 路径 | 说明 |
|------|------|
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

**Windows 内置敏感路径**

| 路径 | 说明 |
|------|------|
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
