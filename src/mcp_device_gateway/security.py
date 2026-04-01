from __future__ import annotations

import re
import shlex
from posixpath import normpath
from pathlib import PurePosixPath

SAFE_ARG_PATTERN = re.compile(r"^[a-zA-Z0-9_./:=+\-]+$")
_SHELL_SEGMENT_SPLIT_PATTERN = re.compile(r"(?:&&|\|\||;|\|)")
_ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")

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
    # 远程代码注入
    (re.compile(r"(curl|wget)\s+[^\n]+\|\s*(ba)?sh", re.IGNORECASE), "禁止从远程拉取并执行脚本"),
    (re.compile(r"base64\s+-d[^\n]+\|\s*(ba)?sh", re.IGNORECASE), "禁止 base64 解码后执行脚本"),
    # 物理内存与内核内存读取
    (re.compile(r"(^|\s)dd\s+[^\n]*\bif=/dev/(mem|kmem)\b", re.IGNORECASE), "禁止读取物理内存或内核内存设备"),
    (re.compile(r"(^|\s)cat\s+/proc/kcore(\s|$|[;&|])", re.IGNORECASE), "禁止读取内核内存转储文件"),
    # SysRq 触发器（可强制触发内核崩溃/重启/同步）
    (re.compile(r"echo\s+[^\n]*>\s*/proc/sysrq-trigger", re.IGNORECASE), "禁止通过 SysRq 触发器操控内核"),
    # 动态库预加载注入（rootkit 常用手法）
    (re.compile(r"\bLD_PRELOAD\s*=", re.IGNORECASE), "禁止通过 LD_PRELOAD 注入动态库"),
    # chmod 符号模式对根目录（补充数字模式未覆盖的情况）
    (re.compile(r"(^|\s)chmod\s+(-R\s+)?[a-zA-Z+=]+(\s+)/(\s|$|[;&|])", re.IGNORECASE), "禁止对根目录修改文件权限（符号模式）"),
    # crontab 写操作（持久化手法；列举 -l 不在拦截范围）
    (re.compile(r"(^|\s)crontab\s+-(e|r)(\s|$|[;&|])", re.IGNORECASE), "禁止通过 crontab 编辑或清除定时任务"),
    (re.compile(r"\|\s*crontab(\s|$|[;&|])", re.IGNORECASE), "禁止通过管道向 crontab 写入定时任务"),
    # bash TCP 重定向反弹 shell
    (re.compile(r"bash\s+[^\n]*>\s*&?\s*/dev/tcp/", re.IGNORECASE), "禁止通过 bash TCP 重定向创建反弹 shell"),
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
    # 防火墙
    (re.compile(r"(^|\s)netsh\s+[^\n]*(firewall|advfirewall)[^\n]*(set[^\n]+off|disable|reset|delete)\b", re.IGNORECASE), "禁止关闭或重置防火墙"),
    # 远程代码注入
    (re.compile(r"(^|\s)(invoke-expression|iex)\s+[\(\$]", re.IGNORECASE), "禁止动态执行代码（Invoke-Expression）"),
    (re.compile(r"-EncodedCommand\b", re.IGNORECASE), "禁止使用 Base64 编码命令（常用于绕过安全检测）"),
    (re.compile(r"(invoke-webrequest|wget|curl)[^\n]+\|\s*(iex|invoke-expression|powershell)", re.IGNORECASE), "禁止从远程拉取并执行脚本"),
    # 权限
    (re.compile(r"(^|\s)(icacls|cacls|takeown)[^\n]+[a-z]:[/\\]windows[/\\]", re.IGNORECASE), "禁止修改 Windows 系统目录权限"),
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


def _normalized_os_family(os_family: str) -> str:
    return (os_family or "linux").strip().lower() or "linux"


def _normalize_remote_path(remote_path: str, os_family: str = "linux") -> str:
    text = str(remote_path).replace("\\", "/").strip()
    if not text:
        return "."
    normalized = normpath(text)
    if text.startswith("//") and not normalized.startswith("//"):
        normalized = "/" + normalized
    if text.startswith("/") and not normalized.startswith("/"):
        normalized = "/" + normalized.lstrip("/")
    normalized = str(PurePosixPath(normalized))
    if _normalized_os_family(os_family) == "windows":
        return normalized.lower()
    return normalized


def _path_prefix_matches(normalized_path: str, normalized_root: str) -> bool:
    return normalized_path == normalized_root or normalized_path.startswith(normalized_root.rstrip("/") + "/")


def _command_references_path(command: str, remote_path: str, os_family: str) -> bool:
    normalized_command = str(command).replace("\\", "/")
    normalized_path = _normalize_remote_path(remote_path, os_family)
    if _normalized_os_family(os_family) == "windows":
        normalized_command = normalized_command.lower()
    if not normalized_path:
        return False
    path_pattern = re.compile(
        rf"(^|[^a-zA-Z0-9_./:-]){re.escape(normalized_path)}(?=$|/|[^a-zA-Z0-9_.-])"
    )
    return path_pattern.search(normalized_command) is not None


def _split_shell_segments(command: str) -> list[str]:
    return [segment.strip() for segment in _SHELL_SEGMENT_SPLIT_PATTERN.split(command) if segment.strip()]


def _tokenize_segment(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _is_env_assignment(token: str) -> bool:
    return _ENV_ASSIGNMENT_PATTERN.fullmatch(token) is not None


def _command_basename(token: str) -> str:
    basename = token.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()
    basename = basename.strip("{}()[]")
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if basename.endswith(suffix):
            return basename[: -len(suffix)]
    return basename


def _is_command_option(args: list[str], option: str) -> bool:
    for arg in args:
        if arg == option:
            return True
        if option.startswith("--") and arg.startswith(f"{option}="):
            return True
        if len(option) == 2 and option.startswith("-") and arg.startswith(option) and len(arg) > 2:
            return True
    return False


def _effective_command(tokens: list[str]) -> tuple[str, list[str]] | None:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        base = _command_basename(token)
        if not base:
            index += 1
            continue
        if _is_env_assignment(token):
            index += 1
            continue
        if base in {"sudo", "nohup", "command"}:
            index += 1
            continue
        if base == "env":
            index += 1
            while index < len(tokens) and _is_env_assignment(tokens[index]):
                index += 1
            continue
        if base == "timeout":
            index += 1
            while index < len(tokens):
                current = tokens[index]
                if current.startswith("-") or re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", current):
                    index += 1
                    continue
                break
            continue
        return base, tokens[index + 1 :]
    return None


def _match_linux_segment_command(
    command: str,
    args: list[str],
    *,
    allow_kernel_module_ops: bool,
) -> str | None:
    if command in {"shutdown", "reboot", "poweroff", "halt"}:
        return "禁止主机关机或重启"
    if command in {"useradd", "userdel", "usermod"}:
        return "禁止修改系统用户"
    if command in {"groupadd", "groupdel", "groupmod"}:
        return "禁止修改系统用户组"
    if command == "passwd":
        return "禁止修改系统账户密码"
    if command in {"insmod", "rmmod", "modprobe"} and not allow_kernel_module_ops:
        return "禁止加载或卸载内核模块"
    if command in {"python", "python2", "python3"} and args and args[0] == "-c":
        return "禁止通过 Python 解释器执行内联代码"
    if command == "perl" and args and args[0] == "-e":
        return "禁止通过 Perl 解释器执行内联代码"
    if command == "ruby" and args and args[0] == "-e":
        return "禁止通过 Ruby 解释器执行内联代码"
    if command == "lua" and args and args[0] == "-e":
        return "禁止通过 Lua 解释器执行内联代码"
    if command in {"nc", "ncat", "netcat"} and "-e" in args and any(
        item.lower() in {"/bin/sh", "/bin/bash"} for item in args
    ):
        return "禁止通过 netcat 创建反弹 shell"
    if command == "sysctl" and _is_command_option(args, "-w"):
        return "禁止直接修改内核运行时参数"
    if command == "visudo":
        return "禁止通过 visudo 编辑 sudoers 权限配置"
    if command in {"flash_erase", "flash_eraseall", "nandwrite", "nanddump"}:
        return "禁止直接操作嵌入式 NAND/NOR Flash（可永久损坏设备）"
    if command in {"ubiformat", "ubirmvol", "ubimkvol", "ubirsvol"}:
        return "禁止操作 UBI Flash 卷（可永久损坏 Flash 分区）"
    if command == "fw_setenv":
        return "禁止修改 U-Boot 引导加载程序环境变量"
    if command == "tcpdump" and _is_command_option(args, "-w"):
        return "禁止通过 tcpdump 将网络流量捕获写入文件"
    if command == "kill" and any(arg == "1" for arg in args):
        return "禁止通过 kill 终止 init/systemd（PID 1）进程"
    if command == "socat" and any(arg.lower().startswith("exec:") for arg in args):
        return "禁止通过 socat 创建交互式 shell 或反弹 shell"
    if command in {"strace", "ltrace"} and (
        _is_command_option(args, "-p") or _is_command_option(args, "--pid")
    ):
        return "禁止通过 strace/ltrace 附加进程（可窃取运行时凭据）"
    if command == "gdb" and (_is_command_option(args, "-p") or _is_command_option(args, "--pid")):
        return "禁止通过 gdb 附加进程（可读取任意进程内存）"
    if command == "nsenter":
        return "禁止通过 nsenter 进入其他进程命名空间（容器逃逸向量）"
    if command == "unshare":
        return "禁止通过 unshare 创建新命名空间（可用于权限逃逸）"
    if command == "at":
        return "禁止通过 at 创建定时任务（常见持久化手法，T1053.001）"
    return None


def _match_windows_segment_command(command: str, args: list[str]) -> str | None:
    if command in {"shutdown", "restart-computer"}:
        return "禁止主机关机或重启"
    if command == "net" and len(args) >= 1 and args[0].lower() == "user":
        return "禁止管理系统用户账户"
    if command == "net" and len(args) >= 2 and args[0].lower() == "localgroup" and any(
        arg.lower() == "/add" for arg in args[1:]
    ):
        return "禁止将用户添加到本地组（可用于提权）"
    if command == "reg" and args and args[0].lower() in {"delete", "add", "import", "export"}:
        return "禁止修改或导出注册表"
    if command == "vssadmin" and len(args) >= 2 and args[0].lower() == "delete" and args[1].lower() == "shadows":
        return "禁止删除卷影副本（防范勒索软件）"
    if command == "wmic" and "shadowcopy" in [arg.lower() for arg in args] and "delete" in [arg.lower() for arg in args]:
        return "禁止通过 wmic 删除卷影副本（防范勒索软件）"
    if command == "set-executionpolicy" and args and args[0].lower() in {"bypass", "unrestricted"}:
        return "禁止绕过 PowerShell 执行策略"
    if command == "schtasks" and any(arg.lower() == "/create" for arg in args):
        return "禁止创建计划任务（常见持久化手法）"
    if command == "wevtutil" and args and args[0].lower() in {"cl", "clear-log"}:
        return "禁止清除 Windows 事件日志（T1070.001）"
    if command == "sc" and args and args[0].lower() in {"create", "config", "delete"}:
        return "禁止创建或修改 Windows 服务（常见持久化手法）"
    if command == "certutil" and any(arg.lower() == "-urlcache" for arg in args):
        return "禁止通过 certutil 下载远程文件（LOL-bin）"
    if command == "mshta":
        return "禁止通过 mshta 执行 HTA 文件（LOL-bin）"
    if command == "rundll32":
        return "禁止通过 rundll32 执行任意 DLL（LOL-bin）"
    if command == "regsvr32":
        return "禁止通过 regsvr32 执行脚本（squiblydoo LOL-bin）"
    if command in {"wscript", "cscript"}:
        return "禁止通过 wscript/cscript 执行脚本（LOL-bin）"
    if command in {"set-mppreference", "add-mppreference"} and any(
        "disable" in arg.lower() or "exclusion" in arg.lower() for arg in args
    ):
        return "禁止通过 PowerShell 禁用 Windows Defender 或添加 AV 排除项"
    return None


def _unwrap_windows_wrapper(command: str, args: list[str]) -> str | None:
    if command == "cmd":
        for index, arg in enumerate(args):
            if arg.lower() in {"/c", "/k"} and index + 1 < len(args):
                return " ".join(args[index + 1 :]).strip()
        return None
    if command in {"powershell", "pwsh"}:
        for index, arg in enumerate(args):
            lowered = arg.lower()
            if lowered in {"-command", "/command", "-c"} and index + 1 < len(args):
                nested = " ".join(args[index + 1 :]).strip().strip('"\'')
                # 兼容 PowerShell ScriptBlock：& { ... }
                if nested.startswith("&"):
                    nested = nested[1:].strip()
                if nested.startswith("{") and nested.endswith("}"):
                    nested = nested[1:-1].strip()
                return nested
        return None
    return None


def sanitize_args(args: list[str]) -> list[str]:
    sanitized: list[str] = []
    for arg in args:
        if not SAFE_ARG_PATTERN.fullmatch(arg):
            raise ValueError(
                f"Unsafe argument '{arg}'. Allowed pattern: {SAFE_ARG_PATTERN.pattern}"
            )
        sanitized.append(arg)
    return sanitized


def is_path_allowed(remote_path: str, allowed_roots: tuple[str, ...], os_family: str = "linux") -> bool:
    normalized = _normalize_remote_path(remote_path, os_family)
    for root in allowed_roots:
        root_norm = _normalize_remote_path(root, os_family)
        if _path_prefix_matches(normalized, root_norm):
            return True
    return False


def is_path_denied(remote_path: str, denied_paths: tuple[str, ...], os_family: str = "linux") -> bool:
    normalized = _normalize_remote_path(remote_path, os_family)
    for blocked in denied_paths:
        blocked_norm = _normalize_remote_path(blocked, os_family)
        if _path_prefix_matches(normalized, blocked_norm):
            return True
    return False


def match_dangerous_command(
    command: str,
    os_family: str,
    *,
    allow_kernel_module_ops: bool = False,
) -> str | None:
    normalized_os = _normalized_os_family(os_family)
    patterns = _LINUX_DANGEROUS_COMMAND_PATTERNS
    for segment in _split_shell_segments(command):
        tokens = _tokenize_segment(segment)
        effective = _effective_command(tokens)
        if effective is None:
            continue
        command_name, args = effective
        if normalized_os == "windows":
            nested_command = _unwrap_windows_wrapper(command_name, args)
            if nested_command:
                nested_reason = match_dangerous_command(
                    nested_command,
                    os_family,
                    allow_kernel_module_ops=allow_kernel_module_ops,
                )
                if nested_reason:
                    return nested_reason
            reason = _match_windows_segment_command(command_name, args)
        else:
            reason = _match_linux_segment_command(
                command_name,
                args,
                allow_kernel_module_ops=allow_kernel_module_ops,
            )
        if reason:
            return reason

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
    normalized_os = _normalized_os_family(os_family)
    normalized = _normalize_remote_path(remote_path, normalized_os)
    paths = _LINUX_BUILTIN_SENSITIVE_PATHS
    if normalized_os == "windows":
        paths = _WINDOWS_BUILTIN_SENSITIVE_PATHS

    for sensitive in paths:
        sensitive_norm = _normalize_remote_path(sensitive, normalized_os)
        if _path_prefix_matches(normalized, sensitive_norm):
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
    normalized_os = _normalized_os_family(os_family)
    builtin_paths = _LINUX_BUILTIN_SENSITIVE_PATHS
    if normalized_os == "windows":
        builtin_paths = _WINDOWS_BUILTIN_SENSITIVE_PATHS

    for sensitive in builtin_paths:
        sensitive_norm = _normalize_remote_path(sensitive, normalized_os)
        if _command_references_path(command, sensitive_norm, normalized_os):
            return f"命令中引用了系统敏感路径 '{sensitive_norm}'，禁止执行"

    if normalized_os == "linux" and re.search(r"(^|[^a-zA-Z0-9_./:-])/dev/mtd\d+($|[^a-zA-Z0-9_.-])", command):
        return "命令中引用了系统敏感路径 '/dev/mtd'，禁止执行"

    for blocked in denied_paths:
        blocked_norm = _normalize_remote_path(blocked, normalized_os).rstrip("/")
        if _command_references_path(command, blocked_norm, normalized_os):
            return f"命令中引用了被禁止的路径 '{blocked_norm}'，禁止执行"

    return None
