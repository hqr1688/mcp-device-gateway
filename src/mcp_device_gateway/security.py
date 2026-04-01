from __future__ import annotations

import re
from pathlib import PurePosixPath

SAFE_ARG_PATTERN = re.compile(r"^[a-zA-Z0-9_./:=+\-]+$")

# ---------- Linux：破坏性操作 ----------
_LINUX_DANGEROUS_COMMAND_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 文件系统与存储
    (re.compile(r"(^|\s)rm\s+-rf\s+/(\s|$|[;&|])", re.IGNORECASE), "禁止删除根目录"),
    (re.compile(r"(^|\s)mkfs(\.|\s)", re.IGNORECASE), "禁止格式化文件系统"),
    (re.compile(r"(^|\s)fdisk\s+", re.IGNORECASE), "禁止修改磁盘分区"),
    (re.compile(r"(^|\s)parted\s+", re.IGNORECASE), "禁止修改磁盘分区"),
    (re.compile(r"(^|\s)dd\s+[^\n]*\bof=/dev/", re.IGNORECASE), "禁止直接写入块设备"),
    # 权限与所有者
    (re.compile(r"(^|\s)chmod\s+(-R\s+)?[0-7]+\s+/(\s|$|[;&|])", re.IGNORECASE), "禁止对根目录修改文件权限"),
    (re.compile(r"(^|\s)chown\s+(-R\s+)?[^\s]+\s+/(\s|$|[;&|])", re.IGNORECASE), "禁止递归修改根目录所有者"),
    # 网络安全
    (re.compile(r"(^|\s)iptables\s+-F(\s|$|[;&|])", re.IGNORECASE), "禁止清空防火墙规则"),
    (re.compile(r"(^|\s)iptables\s+-X(\s|$|[;&|])", re.IGNORECASE), "禁止删除防火墙规则链"),
    (re.compile(r"(^|\s)nft\s+flush\s+ruleset", re.IGNORECASE), "禁止清空 nftables 规则"),
    # 系统控制
    (re.compile(r"(^|\s):\(\)\{\s*:\|:\s*&\s*\};:\s", re.IGNORECASE), "禁止 fork bomb"),
    (re.compile(r"(^|\s)(shutdown|reboot|poweroff|halt)(\s|$|[;&|])", re.IGNORECASE), "禁止主机关机或重启"),
    (re.compile(r"(^|\s)(insmod|rmmod|modprobe)\s+", re.IGNORECASE), "禁止加载或卸载内核模块"),
    # 账户与权限管理
    (re.compile(r"(^|\s)(useradd|userdel|usermod)\s+", re.IGNORECASE), "禁止修改系统用户"),
    (re.compile(r"(^|\s)(groupadd|groupdel|groupmod)\s+", re.IGNORECASE), "禁止修改系统用户组"),
    (re.compile(r"(^|\s)passwd(\s|$|[;&|])", re.IGNORECASE), "禁止修改系统账户密码"),
    # 远程代码注入
    (re.compile(r"(curl|wget)\s+[^\n]+\|\s*(ba)?sh", re.IGNORECASE), "禁止从远程拉取并执行脚本"),
    (re.compile(r"base64\s+-d[^\n]+\|\s*(ba)?sh", re.IGNORECASE), "禁止 base64 解码后执行脚本"),
    # 解释器内联代码注入（常见代码注入路径）
    (re.compile(r"(^|\s)python[23]?\s+-c\b", re.IGNORECASE), "禁止通过 Python 解释器执行内联代码"),
    (re.compile(r"(^|\s)perl\s+-e\b", re.IGNORECASE), "禁止通过 Perl 解释器执行内联代码"),
    (re.compile(r"(^|\s)ruby\s+-e\b", re.IGNORECASE), "禁止通过 Ruby 解释器执行内联代码"),
    (re.compile(r"(^|\s)lua\s+-e\b", re.IGNORECASE), "禁止通过 Lua 解释器执行内联代码"),
    # 反弹 shell（netcat）
    (re.compile(r"(^|\s)(nc|ncat|netcat)\s+[^\n]*-e\s+/bin/(ba)?sh\b", re.IGNORECASE), "禁止通过 netcat 创建反弹 shell"),
    # 物理内存与内核内存读取
    (re.compile(r"(^|\s)dd\s+[^\n]*\bif=/dev/(mem|kmem)\b", re.IGNORECASE), "禁止读取物理内存或内核内存设备"),
    (re.compile(r"(^|\s)cat\s+/proc/kcore(\s|$|[;&|])", re.IGNORECASE), "禁止读取内核内存转储文件"),
    # 内核运行时参数修改
    (re.compile(r"(^|\s)sysctl\s+-w\b", re.IGNORECASE), "禁止直接修改内核运行时参数"),
    # SysRq 触发器（可强制触发内核崩溃/重启/同步）
    (re.compile(r"echo\s+[^\n]*>\s*/proc/sysrq-trigger", re.IGNORECASE), "禁止通过 SysRq 触发器操控内核"),
    # 动态库预加载注入（rootkit 常用手法）
    (re.compile(r"\bLD_PRELOAD\s*=", re.IGNORECASE), "禁止通过 LD_PRELOAD 注入动态库"),
    # chmod 符号模式对根目录（补充数字模式未覆盖的情况）
    (re.compile(r"(^|\s)chmod\s+(-R\s+)?[a-zA-Z+=]+(\s+)/(\s|$|[;&|])", re.IGNORECASE), "禁止对根目录修改文件权限（符号模式）"),
    # crontab 写操作（持久化手法；列举 -l 不在拦截范围）
    (re.compile(r"(^|\s)crontab\s+-(e|r)(\s|$|[;&|])", re.IGNORECASE), "禁止通过 crontab 编辑或清除定时任务"),
    (re.compile(r"\|\s*crontab(\s|$|[;&|])", re.IGNORECASE), "禁止通过管道向 crontab 写入定时任务"),
    # visudo（直接交互式编辑 sudoers，绕过文件路径拦截）
    (re.compile(r"(^|\s)visudo(\s|$|[;&|])", re.IGNORECASE), "禁止通过 visudo 编辑 sudoers 权限配置"),
    # 嵌入式 Flash 操作（CRITICAL：写错分区可永久损坏设备）
    (re.compile(r"(^|\s)(flash_erase|flash_eraseall|nandwrite|nanddump)\s+", re.IGNORECASE), "禁止直接操作嵌入式 NAND/NOR Flash（可永久损坏设备）"),
    (re.compile(r"(^|\s)(ubiformat|ubirmvol|ubimkvol|ubirsvol)\s+", re.IGNORECASE), "禁止操作 UBI Flash 卷（可永久损坏 Flash 分区）"),
    (re.compile(r"(^|\s)fw_setenv\s+", re.IGNORECASE), "禁止修改 U-Boot 引导加载程序环境变量"),
    # 网络流量捕获写文件（可捕获含凭据的流量）
    (re.compile(r"(^|\s)tcpdump\s+[^\n]*-w\s+", re.IGNORECASE), "禁止通过 tcpdump 将网络流量捕获写入文件"),
    # bash TCP 重定向反弹 shell
    (re.compile(r"bash\s+[^\n]*>\s*&?\s*/dev/tcp/", re.IGNORECASE), "禁止通过 bash TCP 重定向创建反弹 shell"),
    # 杀死 init 进程（PID 1），导致系统崩溃
    (re.compile(r"(^|\s)kill\s+(-\d+\s+|-SIG\w+\s+)?1(\s|$|[;&|])", re.IGNORECASE), "禁止通过 kill 终止 init/systemd（PID 1）进程"),
    # socat 反弹 shell
    (re.compile(r"(^|\s)socat\s+[^\n]*\bexec:", re.IGNORECASE), "禁止通过 socat 创建交互式 shell 或反弹 shell"),
    # 进程内存读取（凭据窃取）
    (re.compile(r"(^|\s)(strace|ltrace)\s+(-p|--pid)\b", re.IGNORECASE), "禁止通过 strace/ltrace 附加进程（可窃取运行时凭据）"),
    (re.compile(r"(^|\s)gdb\s+[^\n]*(-p|--pid)\b", re.IGNORECASE), "禁止通过 gdb 附加进程（可读取任意进程内存）"),
    # 容器/命名空间逃逸
    (re.compile(r"(^|\s)nsenter\s+", re.IGNORECASE), "禁止通过 nsenter 进入其他进程命名空间（容器逃逸向量）"),
    (re.compile(r"(^|\s)unshare\s+", re.IGNORECASE), "禁止通过 unshare 创建新命名空间（可用于权限逃逸）"),
    # at 定时任务（持久化手法）
    (re.compile(r"(^|\s)at(\s+|$)", re.IGNORECASE), "禁止通过 at 创建定时任务（常见持久化手法，T1053.001）"),
)

# ---------- Windows：破坏性操作 ----------
_WINDOWS_DANGEROUS_COMMAND_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 磁盘与引导
    (re.compile(r"(^|\s)format\s+[a-z]:", re.IGNORECASE), "禁止格式化磁盘"),
    (re.compile(r"(^|\s)diskpart(\s|$|[;&|])", re.IGNORECASE), "禁止执行磁盘分区工具"),
    (re.compile(r"(^|\s)bcdedit\b", re.IGNORECASE), "禁止修改启动配置"),
    # 文件删除
    (re.compile(r"remove-item\s+[^\n]*-recurse[^\n]*-force[^\n]*[a-z]:[/\\]", re.IGNORECASE), "禁止强制递归删除系统盘路径"),
    (re.compile(r"(^|\s)del\s+/f\s+/s\s+/q\s+[a-z]:[/\\]", re.IGNORECASE), "禁止强制递归删除系统盘路径"),
    # 系统控制
    (re.compile(r"(^|\s)(shutdown|restart-computer)\b", re.IGNORECASE), "禁止主机关机或重启"),
    # 账户与注册表
    (re.compile(r"(^|\s)net\s+user\b", re.IGNORECASE), "禁止管理系统用户账户"),
    (re.compile(r"(^|\s)reg\s+(delete|add|import|export)\b", re.IGNORECASE), "禁止修改或导出注册表"),
    # 防火墙
    (re.compile(r"(^|\s)netsh\s+[^\n]*(firewall|advfirewall)[^\n]*(set[^\n]+off|disable|reset|delete)\b", re.IGNORECASE), "禁止关闭或重置防火墙"),
    # 远程代码注入
    (re.compile(r"(^|\s)(invoke-expression|iex)\s+[\(\$]", re.IGNORECASE), "禁止动态执行代码（Invoke-Expression）"),
    (re.compile(r"-EncodedCommand\b", re.IGNORECASE), "禁止使用 Base64 编码命令（常用于绕过安全检测）"),
    (re.compile(r"(invoke-webrequest|wget|curl)[^\n]+\|\s*(iex|invoke-expression|powershell)", re.IGNORECASE), "禁止从远程拉取并执行脚本"),
    # 权限
    (re.compile(r"(^|\s)(icacls|cacls|takeown)[^\n]+[a-z]:[/\\]windows[/\\]", re.IGNORECASE), "禁止修改 Windows 系统目录权限"),
    # 卷影副本删除（勒索软件标志性手法）
    (re.compile(r"(^|\s)vssadmin\s+delete\s+shadows\b", re.IGNORECASE), "禁止删除卷影副本（防范勒索软件）"),
    (re.compile(r"(^|\s)wmic\s+[^\n]*shadowcopy[^\n]*delete\b", re.IGNORECASE), "禁止通过 wmic 删除卷影副本（防范勒索软件）"),
    # PowerShell 执行策略绕过
    (re.compile(r"(^|\s)Set-ExecutionPolicy\s+(Bypass|Unrestricted)\b", re.IGNORECASE), "禁止绕过 PowerShell 执行策略"),
    # 计划任务创建（持久化手法）
    (re.compile(r"(^|\s)schtasks\s+/create\b", re.IGNORECASE), "禁止创建计划任务（常见持久化手法）"),
    # 事件日志清除（勒索软件/APT 常见证据销毁手法）
    (re.compile(r"(^|\s)wevtutil\s+(cl|clear-log)\b", re.IGNORECASE), "禁止清除 Windows 事件日志（T1070.001）"),
    # Windows 服务创建与修改（持久化手法）
    (re.compile(r"(^|\s)sc\s+(create|config|delete)\b", re.IGNORECASE), "禁止创建或修改 Windows 服务（常见持久化手法）"),
    # certutil LOL-bin 文件下载（常用于绕过杀毒检测）
    (re.compile(r"(^|\s)certutil\s+[^\n]*-urlcache\b", re.IGNORECASE), "禁止通过 certutil 下载远程文件（LOL-bin）"),
    # 提权：将用户加入管理员组
    (re.compile(r"(^|\s)net\s+localgroup[^\n]*/add\b", re.IGNORECASE), "禁止将用户添加到本地组（可用于提权）"),
    # mshta LOL-bin（执行 HTA 文件，常见代码执行绕过手法）
    (re.compile(r"(^|\s)mshta\s+", re.IGNORECASE), "禁止通过 mshta 执行 HTA 文件（LOL-bin）"),
    # rundll32 LOL-bin（加载攻击者控制的 DLL）
    (re.compile(r"(^|\s)rundll32\s+", re.IGNORECASE), "禁止通过 rundll32 执行任意 DLL（LOL-bin）"),
    # regsvr32 squiblydoo LOL-bin（可远程加载 COM scriptlet，绕过 AppLocker）
    (re.compile(r"(^|\s)regsvr32\s+", re.IGNORECASE), "禁止通过 regsvr32 执行脚本（squiblydoo LOL-bin）"),
    # Windows Script Host LOL-bin
    (re.compile(r"(^|\s)(wscript|cscript)\s+", re.IGNORECASE), "禁止通过 wscript/cscript 执行脚本（LOL-bin）"),
    # 禁用 Windows Defender / AV 实时保护
    (re.compile(r"(^|\s)(Set-MpPreference|Add-MpPreference)\s+[^\n]*(Disable|Exclusion)", re.IGNORECASE), "禁止通过 PowerShell 禁用 Windows Defender 或添加 AV 排除项"),
)

# ---------- 内置敏感路径：Linux ----------
# 这些路径始终被拦截，不依赖 denied_paths 配置。
_LINUX_BUILTIN_SENSITIVE_PATHS: tuple[str, ...] = (
    "/etc/shadow",        # 系统密码哈希
    "/etc/gshadow",       # 用户组密码哈希
    "/etc/sudoers",       # sudo 权限配置
    "/etc/sudoers.d",     # sudo 权限扩展目录
    "/root/.ssh",         # root SSH 密钥目录
    "/root/.gnupg",       # root GPG 密钥目录
    "/etc/ssl/private",   # TLS/SSL 私钥目录
    "/etc/pki",           # PKI 密钥目录（RHEL/CentOS 系）
    "/proc/1/environ",    # init 进程环境变量（常含明文密钥/密码）
    "/etc/ld.so.preload",  # 动态库预加载配置（rootkit 注入的最常见向量）
    "/etc/crontab",        # 系统级定时任务（常被用于持久化）
    "/etc/cron.d",         # 定时任务扩展目录（持久化手法）
    "/var/spool/cron",     # 用户定时任务目录（持久化手法）
    "/etc/ssh/sshd_config", # SSH 服务端配置（直接影响远程访问安全）
    "/boot",               # 引导加载程序与内核镜像（修改可导致设备无法启动）
    "/dev/mem",            # 物理内存设备（可绕过权限读取任意内存内容）
    "/dev/kmem",           # 内核内存设备（可读取内核态数据）
    "/proc/kcore",         # 内核内存转储（含完整内核地址空间数据）
    "/etc/passwd",         # 用户账户信息（枚举用户/写入可植入后门账户）
    "/etc/hosts",          # 本地 DNS 解析（修改可实施 DNS 劫持）
    "/etc/profile.d",      # Shell 初始化脚本目录（持久化注入）
    "/etc/init.d",         # init 启动脚本（持久化手法）
    "/etc/systemd/system", # systemd 服务单元文件（持久化手法）
    "/etc/rc.local",       # 开机自启动脚本（持久化手法）
    "/dev/mtd",            # 嵌入式 MTD Flash 设备节点（写入可永久损坏设备）
    "/etc/pam.d",          # PAM 身份验证配置（修改可实现认证绕过）
    "/etc/security",       # PAM 安全策略配置（修改可提权或绕过限制）
    "/etc/ld.so.conf",     # 动态链接库搜索路径（修改可实现库注入）
)

# ---------- 内置敏感路径：Windows ----------
# Windows 路径遵循 POSIX 斜杠规范化（与 sanitize_args 保持一致）。
_WINDOWS_BUILTIN_SENSITIVE_PATHS: tuple[str, ...] = (
    "C:/Windows/System32/config",          # SAM/SECURITY/SYSTEM hive（凭据核心）
    "C:/Windows/NTDS",                     # Active Directory 数据库
    "C:/Users/All Users/Microsoft/Crypto",                        # 系统密钥材料
    "C:/Windows/System32/drivers/etc/hosts",                      # hosts 文件（可用于 DNS 劫持）
    "C:/ProgramData/Microsoft/Windows/Start Menu/Programs/Startup", # 自启动目录（持久化手法）
    "C:/Windows/System32/Tasks",                                     # 计划任务 XML 文件目录（持久化手法）
    "C:/Windows/System32/winevt/Logs",                               # 事件日志文件（防止日志篡改/删除）
)


def sanitize_args(args: list[str]) -> list[str]:
    sanitized: list[str] = []
    for arg in args:
        if not SAFE_ARG_PATTERN.fullmatch(arg):
            raise ValueError(
                f"Unsafe argument '{arg}'. Allowed pattern: {SAFE_ARG_PATTERN.pattern}"
            )
        sanitized.append(arg)
    return sanitized


def is_path_allowed(remote_path: str, allowed_roots: tuple[str, ...]) -> bool:
    normalized = str(PurePosixPath(remote_path))
    for root in allowed_roots:
        root_norm = str(PurePosixPath(root))
        if normalized == root_norm or normalized.startswith(root_norm.rstrip("/") + "/"):
            return True
    return False


def is_path_denied(remote_path: str, denied_paths: tuple[str, ...]) -> bool:
    normalized = str(PurePosixPath(remote_path))
    for blocked in denied_paths:
        blocked_norm = str(PurePosixPath(blocked))
        if normalized == blocked_norm or normalized.startswith(blocked_norm.rstrip("/") + "/"):
            return True
    return False


def match_dangerous_command(command: str, os_family: str) -> str | None:
    normalized_os = (os_family or "linux").strip().lower() or "linux"
    patterns = _LINUX_DANGEROUS_COMMAND_PATTERNS
    if normalized_os == "windows":
        patterns = _WINDOWS_DANGEROUS_COMMAND_PATTERNS

    for pattern, reason in patterns:
        if pattern.search(command):
            return reason
    return None


def match_sensitive_path(remote_path: str, os_family: str) -> str | None:
    """检查路径是否命中内置敏感路径列表（不依赖 denied_paths 配置）。

    命中时返回拒绝原因，否则返回 None。
    """
    normalized = str(PurePosixPath(remote_path))
    normalized_os = (os_family or "linux").strip().lower() or "linux"
    paths = _LINUX_BUILTIN_SENSITIVE_PATHS
    if normalized_os == "windows":
        paths = _WINDOWS_BUILTIN_SENSITIVE_PATHS

    for sensitive in paths:
        sensitive_norm = str(PurePosixPath(sensitive))
        if normalized == sensitive_norm or normalized.startswith(sensitive_norm.rstrip("/") + "/"):
            return f"路径 '{remote_path}' 包含系统敏感信息，禁止访问"
    return None


def scan_command_for_sensitive_paths(
    command: str,
    os_family: str,
    denied_paths: tuple[str, ...] = (),
) -> str | None:
    """扫描渲染后的命令字符串，检查是否引用了内置敏感路径或配置中禁止的路径。

    采用子串匹配：只要敏感/禁止路径的规范化字符串出现在命令中即视为命中。
    命中时返回拒绝原因，否则返回 None。
    """
    normalized_os = (os_family or "linux").strip().lower() or "linux"
    builtin_paths = _LINUX_BUILTIN_SENSITIVE_PATHS
    if normalized_os == "windows":
        builtin_paths = _WINDOWS_BUILTIN_SENSITIVE_PATHS

    for sensitive in builtin_paths:
        sensitive_norm = str(PurePosixPath(sensitive))
        if sensitive_norm in command:
            return f"命令中引用了系统敏感路径 '{sensitive_norm}'，禁止执行"

    for blocked in denied_paths:
        blocked_norm = str(PurePosixPath(blocked)).rstrip("/")
        if blocked_norm in command:
            return f"命令中引用了被禁止的路径 '{blocked_norm}'，禁止执行"

    return None
