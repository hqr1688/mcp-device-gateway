from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
import paramiko
import yaml

from .config import AppConfig, CommandTemplate, DeviceConfig, load_config
from .security import is_path_allowed, is_path_denied, match_dangerous_command, match_sensitive_path, scan_command_for_sensitive_paths, sanitize_args
from .ssh_client import SshDeviceClient

mcp = FastMCP("embedded-device-gateway")
_CONFIG_LOCK = Lock()
_CONFIG_STATE: dict[str, Any] = {
    "cache": None,
    "fingerprint": None,
    "last_error": None,
    "available": False,
}

# 异步任务存储：job_id -> 任务状态字典
_JOBS_LOCK = Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_MAX = 200  # 最多保留任务数，超出时清理已完成任务
_EXEC_OPERATION_ERRORS = (paramiko.SSHException, socket.error, EOFError)

_DEFAULT_CONFIG_TEMPLATE = """# mcp-device-gateway 自动生成配置模板（请按实际环境修改）
# 说明：当前模板故意保持 devices 为空，服务会拒绝业务工具调用，直到你补齐至少一个设备。
devices: {}

command_templates: {}
"""


def _get_config_fingerprint() -> tuple[Path, bool, int, int]:
    config_path = Path(os.getenv("MCP_DEVICE_CONFIG", "./devices.example.yaml")).resolve()
    if not config_path.exists() or not config_path.is_file():
        return config_path, False, 0, 0

    stat = config_path.stat()
    return config_path, True, stat.st_mtime_ns, stat.st_size


def _load_config_safely(fingerprint: tuple[Path, bool, int, int]) -> tuple[AppConfig | None, str | None]:
    config_path, exists, _, _ = fingerprint
    if not exists:
        return None, f"Config file not found: {config_path}"

    try:
        return load_config(), None
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        return None, f"Failed to load config: {exc}"


def _ensure_default_config_file(config_path: Path) -> str | None:
    if config_path.exists():
        return None

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(_DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
        return f"Auto-generated config template: {config_path}，请尽快更新后保存。"
    except OSError as exc:
        return f"Failed to auto-generate config template: {exc}"


def _refresh_config_state(force: bool = False) -> None:
    fingerprint = _get_config_fingerprint()
    config_path, exists, _, _ = fingerprint
    generated_message: str | None = None

    if not exists:
        generated_message = _ensure_default_config_file(config_path)
        fingerprint = _get_config_fingerprint()

    with _CONFIG_LOCK:
        old_fingerprint = _CONFIG_STATE["fingerprint"]
        old_error = _CONFIG_STATE["last_error"]
        old_available = _CONFIG_STATE["available"]

        if not force and old_fingerprint == fingerprint:
            if generated_message and generated_message != old_error:
                print(f"[WARN] {generated_message}", file=sys.stderr)
            return

        cached, error = _load_config_safely(fingerprint)
        _CONFIG_STATE["cache"] = cached
        _CONFIG_STATE["fingerprint"] = fingerprint
        _CONFIG_STATE["last_error"] = error
        _CONFIG_STATE["available"] = error is None and cached is not None

        new_error = _CONFIG_STATE["last_error"]
        new_available = _CONFIG_STATE["available"]

    if generated_message:
        print(f"[WARN] {generated_message}", file=sys.stderr)

    if new_error and new_error != old_error:
        print(f"[WARN] {new_error}。业务工具调用将被拒绝。", file=sys.stderr)
    if new_available and not old_available:
        print("[INFO] 配置文件已恢复可用，已自动重新加载。", file=sys.stderr)


def _config_monitor_loop() -> None:
    interval_text = os.getenv("MCP_CONFIG_POLL_INTERVAL_SEC", "2")
    try:
        interval_sec = max(0.5, float(interval_text))
    except ValueError:
        interval_sec = 2.0

    while True:
        try:
            _refresh_config_state()
        except (OSError, ValueError, TypeError) as exc:
            print(f"[WARN] Config monitor error: {exc}", file=sys.stderr)
        time.sleep(interval_sec)


def _get_app_config() -> AppConfig:
    _refresh_config_state()
    with _CONFIG_LOCK:
        cached = _CONFIG_STATE["cache"]
        if cached is None:
            error = _CONFIG_STATE["last_error"] or "Config is unavailable."
            raise RuntimeError(f"CONFIG_UNAVAILABLE: 配置不可用：{error} 请更新配置文件后重试。")

        return cached


def _audit(tool: str, payload: dict[str, Any]) -> None:
    # audit_log 路径由 MCP_AUDIT_LOG 环境变量决定，与 AppConfig 无关，
    # 直接读取以避免配置不可用时 _get_app_config() 抛出异常掩盖已完成的操作。
    log_path = Path(os.getenv("MCP_AUDIT_LOG", "./mcp_audit.log"))
    line: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "payload": payload,
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def _get_device(device_name: str) -> DeviceConfig:
    app_config = _get_app_config()
    try:
        return app_config.devices[device_name]
    except KeyError as exc:
        raise ValueError(f"UNKNOWN_DEVICE: Unknown device '{device_name}'.") from exc


def _template_arg_count(template: str) -> int:
    indexes = [int(m.group(1)) for m in re.finditer(r"\{(\d+)\}", template)]
    if not indexes:
        return 0
    return max(indexes) + 1


def _serialize_template(key: str, item: CommandTemplate) -> dict[str, Any]:
    return {
        "key": key,
        "short_key": item.short_key,
        "template": item.template,
        "description": item.description,
        "when_to_use": item.when_to_use,
        "args": list(item.args),
        "examples": [list(e) for e in item.examples],
        "arg_count": _template_arg_count(item.template),
        "risk": item.risk,
        "exec_mode": item.exec_mode,
        "category": item.category,
        "device_name": item.device_name,
    }


def _serialize_device(cfg: DeviceConfig) -> dict[str, Any]:
    return {
        "name": cfg.name,
        "host": cfg.host,
        "port": cfg.port,
        "username": cfg.username,
        "os_family": cfg.os_family,
        "os_name": cfg.os_name,
        "os_version": cfg.os_version,
        "allowed_roots": list(cfg.allowed_roots),
        "denied_paths": list(cfg.denied_paths),
        "description": cfg.description,
        "when_to_use": cfg.when_to_use,
        "capabilities": list(cfg.capabilities),
        "tags": list(cfg.tags),
        "preferred_templates": list(cfg.preferred_templates),
    }


def _guard_remote_path(cfg: DeviceConfig, remote_path: str) -> None:
    # 内置敏感路径始终拦截，优先级最高，不受任何配置覆盖。
    sensitive_reason = match_sensitive_path(remote_path, cfg.os_family)
    if sensitive_reason:
        raise ValueError(f"SENSITIVE_PATH_BLOCKED: {sensitive_reason}。")

    if cfg.denied_paths and is_path_denied(remote_path, cfg.denied_paths):
        raise ValueError(f"REMOTE_PATH_DENIED: Remote path is denied: {remote_path}")

    # 空 allowed_roots 表示不限制路径，此时仅应用 denied_paths 黑名单。
    if cfg.allowed_roots and not is_path_allowed(remote_path, cfg.allowed_roots):
        raise ValueError(f"REMOTE_PATH_NOT_ALLOWED: Remote path is not allowed: {remote_path}")


def _guard_command_safety(command: str, cfg: DeviceConfig) -> None:
    reason = match_dangerous_command(command, cfg.os_family)
    if reason:
        raise ValueError(f"DANGEROUS_COMMAND_BLOCKED: {reason}。命令已被内置安全策略拦截。")

    path_reason = scan_command_for_sensitive_paths(command, cfg.os_family, cfg.denied_paths)
    if path_reason:
        raise ValueError(f"SENSITIVE_PATH_BLOCKED: {path_reason}。命令已被内置安全策略拦截。")


def _guess_cmd_args(item: CommandTemplate) -> list[str]:
    if item.examples:
        return list(item.examples[0])
    if item.args:
        return [f"<{name}>" for name in item.args]

    count = _template_arg_count(item.template)
    return [f"<arg{i}>" for i in range(count)]


def _score_template(task_text: str, key: str, item: CommandTemplate) -> int:
    haystack = " ".join([key, item.description, item.when_to_use, item.template]).lower()
    score = 0
    tokens = [tok for tok in re.split(r"[^a-z0-9_\u4e00-\u9fff]+", task_text.lower()) if tok]
    for tok in tokens:
        if len(tok) < 2:
            continue
        if tok in haystack:
            score += 3

    if key.lower() in task_text.lower():
        score += 10

    if any(word in task_text for word in ["日志", "log"]) and "log" in key.lower():
        score += 5
    if any(word in task_text for word in ["状态", "status"]) and "status" in key.lower():
        score += 5
    if any(word in task_text for word in ["重启", "restart"]) and "restart" in key.lower():
        score += 5
    if any(word in task_text for word in ["目录", "list", "ls"]) and ("list" in key.lower() or "ls" in item.template.lower()):
        score += 4

    return score


_CAPABILITY_TOOLS: list[dict[str, str]] = [
    {"name": "capability_overview", "when": "首次接入时查看服务能力地图、自检结果与推荐工作流"},
    {"name": "device_list", "when": "获取设备清单"},
    {"name": "device_profile_get", "when": "查看设备能力画像"},
    {"name": "device_ping", "when": "执行前连通性检查"},
    {"name": "command_template_list", "when": "发现可执行命令模板"},
    {"name": "command_template_get", "when": "查看命令模板详情（含 exec_mode 字段）"},
    {"name": "task_recommend", "when": "根据自然语言任务生成工具调用建议（自动选择执行模式）"},
    {"name": "cmd_exec", "when": "同步执行单条命令（exec_mode==sync）"},
    {"name": "cmd_exec_batch", "when": "并发执行多条无依赖同步命令（exec_mode==sync）"},
    {"name": "cmd_exec_async", "when": "异步提交长耗时命令（exec_mode==async），立即返回 job_id"},
    {"name": "cmd_exec_result", "when": "取回 cmd_exec_async 的执行结果，status=done 时获得完整输出"},
    {"name": "dir_list", "when": "列出远端目录内容"},
    {"name": "file_stat", "when": "查询远端文件或目录的元信息"},
    {"name": "file_upload", "when": "上传本地文件到设备"},
    {"name": "file_download", "when": "下载设备文件到本地"},
]

_CAPABILITY_RESOURCES: list[dict[str, str]] = [
    {"name": "config://summary", "when": "读取当前配置摘要，包括设备数、模板数、已加载设备与模板键"},
]

_CAPABILITY_PROMPTS: list[dict[str, str]] = [
    {"name": "device_ops_prompt", "when": "根据任务和设备生成推荐调用顺序与工具选择提示"},
]


@mcp.resource("config://summary")
def config_summary_resource() -> dict[str, Any]:
    """读取网关配置摘要资源。"""
    app_config = _get_app_config()
    devices = sorted(app_config.devices.keys())
    templates = sorted(app_config.command_templates.keys())
    data: dict[str, Any] = {
        "service": "embedded-device-gateway",
        "config_path": str(Path(os.getenv("MCP_DEVICE_CONFIG", "./devices.example.yaml")).resolve()),
        "device_count": len(devices),
        "template_count": len(templates),
        "devices": devices,
        "templates": templates,
    }
    _audit("resource.config.summary", {"device_count": len(devices), "template_count": len(templates)})
    return data


@mcp.prompt()
def device_ops_prompt(task: str, device_name: str | None = None) -> str:
    """生成设备操作建议提示模板。"""
    app_config = _get_app_config()
    known_devices = sorted(app_config.devices.keys())
    target_device = device_name or "<请先调用 device_list 选择设备>"
    prompt_text = (
        "你是嵌入式设备运维助手。请按以下顺序执行：\n"
        "1. 先调用 device_list 确认可用设备。\n"
        "2. 若用户指定设备，再调用 device_profile_get 查看能力与推荐模板。\n"
        "3. 执行前先调用 device_ping 做连通性检查。\n"
        "4. 根据任务内容优先调用 task_recommend 与 command_template_list 选模板。\n"
        "5. 执行命令时必须按 3 种模式选择工具：\n"
        "   - sync（单条同步）：使用 cmd_exec，适合秒级命令并立即读取结果。\n"
        "   - batch（多条并发同步）：使用 cmd_exec_batch，适合多条互不依赖的短命令。\n"
        "   - async（长任务异步）：先用 cmd_exec_async 提交，再用 cmd_exec_result 轮询到 done/error。\n"
        "6. 文件传输场景使用 file_upload/file_download，并在返回中包含风险说明与下一步建议。\n\n"
        f"用户任务：{task}\n"
        f"目标设备：{target_device}\n"
        f"当前已知设备：{', '.join(known_devices) if known_devices else '（无）'}"
    )
    _audit("prompt.device.ops", {"task": task, "device_name": device_name or ""})
    return prompt_text


@mcp.tool()
def device_list() -> list[dict[str, Any]]:
    """列出所有设备。

    何时使用：
    - 执行任何设备相关操作前先调用本工具。
    - 结合 command_template_list 决定后续 cmd_exec 参数。
    """
    app_config = _get_app_config()
    data: list[dict[str, Any]] = [_serialize_device(cfg) for cfg in app_config.devices.values()]
    _audit("device.list", {"count": len(data)})
    return data


@mcp.tool()
def device_profile_get(device_name: str) -> dict[str, Any]:
    """获取单个设备画像（用途、能力、推荐模板）。

    何时使用：
    - 已知设备名，想确认该设备最适合执行什么任务时。
    """
    cfg = _get_device(device_name)
    data = _serialize_device(cfg)
    _audit("device.profile.get", {"device": device_name})
    return data


@mcp.tool()
def device_ping(device_name: str) -> dict[str, Any]:
    """测试设备 SSH 连通性。

    何时使用：
    - 执行 cmd_exec/file_upload/file_download 前做预检查。
    """
    cfg = _get_device(device_name)
    # ping 使用独立连接，不污染连接池
    client = SshDeviceClient(cfg, use_pool=False)
    ok = client.ping()

    result: dict[str, Any] = {"device": device_name, "reachable": ok}
    _audit("device.ping", result)
    return result


@mcp.tool()
def command_template_list() -> dict[str, Any]:
    """列出所有命令模板及用途说明。

    何时使用：
    - 不确定 command_key 时优先调用。
    - 需要确认参数数量、风险级别与适用场景时调用。
    """
    app_config = _get_app_config()
    templates: list[dict[str, Any]] = [_serialize_template(k, v) for k, v in app_config.command_templates.items()]

    result: dict[str, Any] = {
        "count": len(templates),
        "templates": templates,
        "args_sanitize_pattern": "^[a-zA-Z0-9_./:\\=+\\-]+$",
        "recommended_workflow": [
            "device_list",
            "device_ping",
            "command_template_list",
            "cmd_exec",
        ],
    }
    _audit("command_template.list", {"count": len(templates)})
    return result


@mcp.tool()
def command_template_get(command_key: str) -> dict[str, Any]:
    """获取单个命令模板详情。

    何时使用：
    - 已确定 command_key，需要查看参数与风险信息时调用。
    """
    app_config = _get_app_config()
    item = app_config.command_templates.get(command_key)
    if item is None:
        raise ValueError(f"UNKNOWN_COMMAND_TEMPLATE: Unknown command template '{command_key}'.")

    data: dict[str, Any] = _serialize_template(command_key, item)
    _audit("command_template.get", {"command_key": command_key})
    return data


@mcp.tool()
def capability_overview() -> dict[str, Any]:
    """返回服务能力地图与推荐调用顺序。

    何时使用：
    - Agent 初次连接本 MCP 服务时。
    - 任务中途不确定该用哪个工具时。
    """
    app_config = _get_app_config()
    data: dict[str, Any] = {
        "service": "embedded-device-gateway",
        "config_available": True,
        "device_count": len(app_config.devices),
        "template_count": len(app_config.command_templates),
        "tool_count": len(_CAPABILITY_TOOLS),
        "resource_count": len(_CAPABILITY_RESOURCES),
        "prompt_count": len(_CAPABILITY_PROMPTS),
        "workflow": [
            "先调用 device_list 获取设备",
            "必要时调用 device_profile_get 查看设备画像与推荐模板",
            "调用 device_ping 验证连通性",
            "调用 command_template_list 或 command_template_get 选择命令",
            "如只知道自然语言任务，可先调用 task_recommend 生成调用草案",
            "调用 cmd_exec 执行远端命令（同步阻塞）",
            "长耗时命令用 cmd_exec_async 异步提交，再用 cmd_exec_result 轮询结果",
            "批量并发执行多条无依赖命令时使用 cmd_exec_batch",
            "需要浏览目录时使用 dir_list；查询单个文件属性使用 file_stat",
            "需要传输文件时使用 file_upload/file_download",
        ],
        "exec_mode_guide": {
            "decision_rule": "执行任何命令前，先调用 command_template_get 查看模板的 exec_mode 字段，再按下表选择工具。",
            "modes": [
                {
                    "exec_mode": "sync",
                    "characteristics": "命令秒级完成，需立即取回结果",
                    "single_command": "cmd_exec",
                    "multiple_independent_commands": "cmd_exec_batch（并发执行，一次取回全部结果）",
                    "do_not_use": "cmd_exec_async（不必要的异步开销）",
                    "typical_examples": ["查看进程", "读取日志最后几行", "health_check", "查磁盘"],
                },
                {
                    "exec_mode": "async",
                    "characteristics": "命令耗时长（数秒至数分钟），不应阻塞 Agent 主流程",
                    "tool_sequence": ["cmd_exec_async → 立即得到 job_id", "cmd_exec_result(job_id) → 轮询直到 status!=running"],
                    "do_not_use": "cmd_exec / cmd_exec_batch（会长时间阻塞，可能超时）",
                    "typical_examples": ["run_idm（nohup 重启进程）", "vm_make（编译）", "vm_deploy（scp 传输）"],
                },
            ],
            "shortcut": "如只有自然语言任务描述，调用 task_recommend —— 它会自动读取 exec_mode 并在 recommended_tool 字段直接告知应使用哪个工具。",
        },
        "tools": [dict(item) for item in _CAPABILITY_TOOLS],
        "resources": [dict(item) for item in _CAPABILITY_RESOURCES],
        "prompts": [dict(item) for item in _CAPABILITY_PROMPTS],
    }
    _audit("capability.overview", {"device_count": len(app_config.devices), "template_count": len(app_config.command_templates)})
    return data


@mcp.tool()
def task_recommend(task: str) -> dict[str, Any]:
    """根据自然语言任务，返回推荐工具与参数草案。

    何时使用：
    - 已知任务目标，但不确定该用哪个工具/命令模板时。
    """
    app_config = _get_app_config()
    text = task.strip()
    lowered = text.lower()

    if any(word in lowered for word in ["upload", "上传", "下发"]):
        result: dict[str, Any] = {
            "task": task,
            "recommended_tool": "file_upload",
            "draft": {
                "device_name": "<device_name>",
                "local_path": "<local_path>",
                "remote_path": "<remote_path>",
            },
            "reason": "任务语义更接近文件下发。",
            "next_steps": ["device_list", "device_ping", "file_upload"],
        }
        _audit("task.recommend", {"tool": "file_upload"})
        return result

    if any(word in lowered for word in ["download", "下载", "拉取", "获取日志"]):
        result: dict[str, Any] = {
            "task": task,
            "recommended_tool": "file_download",
            "draft": {
                "device_name": "<device_name>",
                "remote_path": "<remote_path>",
                "local_path": "<local_path>",
            },
            "reason": "任务语义更接近文件回收。",
            "next_steps": ["device_list", "device_ping", "file_download"],
        }
        _audit("task.recommend", {"tool": "file_download"})
        return result

    if any(word in lowered for word in ["ping", "连通", "可达"]):
        result: dict[str, Any] = {
            "task": task,
            "recommended_tool": "device_ping",
            "draft": {"device_name": "<device_name>"},
            "reason": "任务语义是连通性检查。",
            "next_steps": ["device_list", "device_ping"],
        }
        _audit("task.recommend", {"tool": "device_ping"})
        return result

    best_key = ""
    best_item: CommandTemplate | None = None
    best_score = -1
    for key, item in app_config.command_templates.items():
        score = _score_template(text, key, item)
        if score > best_score:
            best_score = score
            best_key = key
            best_item = item

    if best_item is not None and best_score > 0:
        suggested_device = ""
        for device in app_config.devices.values():
            if best_key in device.preferred_templates:
                suggested_device = device.name
                break

        use_async = best_item.exec_mode == "async"
        recommended_tool = "cmd_exec_async" if use_async else "cmd_exec"
        draft: dict[str, Any] = {
            "device_name": suggested_device or "<device_name>",
            "command_key": best_key,
            "args": _guess_cmd_args(best_item),
            "timeout_sec": 30,
        }
        next_steps = ["device_list", "device_profile_get", "device_ping", "command_template_get", recommended_tool]
        if use_async:
            next_steps.append("cmd_exec_result")

        result: dict[str, Any] = {
            "task": task,
            "recommended_tool": recommended_tool,
            "draft": draft,
            "matched_template": _serialize_template(best_key, best_item),
            "suggested_device": suggested_device or None,
            "reason": f"根据模板关键词和用途说明匹配得到（exec_mode={best_item.exec_mode}）。",
            "next_steps": next_steps,
        }
        _audit("task.recommend", {"tool": recommended_tool, "command_key": best_key})
        return result

    result: dict[str, Any] = {
        "task": task,
        "recommended_tool": "capability_overview",
        "draft": {},
        "reason": "未匹配到高置信度模板，建议先查看能力地图与模板列表。",
        "next_steps": ["capability_overview", "command_template_list"],
    }
    _audit("task.recommend", {"tool": "capability_overview"})
    return result


@mcp.tool()
def cmd_exec(device_name: str, command_key: str, args: list[str] | None = None, timeout_sec: int = 30) -> dict[str, Any]:
    """执行单条命令模板（同步阻塞）。

    何时使用：
    - 模板的 exec_mode == "sync"，且只需执行一条命令时。
    - 需要立即取回 stdout/stderr/exit_code 时。

    何时不用：
    - 模板的 exec_mode == "async" → 改用 cmd_exec_async（否则可能阻塞超时）。
    - 需要并发执行多条独立命令 → 改用 cmd_exec_batch（更高效）。
    """
    cfg = _get_device(device_name)
    template_item = _get_app_config().command_templates.get(command_key)
    if not template_item:
        raise ValueError(f"UNKNOWN_COMMAND_TEMPLATE: Unknown command template '{command_key}'.")

    if template_item.category == "device_specific" and template_item.device_name != device_name:
        raise ValueError(
            f"TEMPLATE_NOT_APPLICABLE: Template '{command_key}' is only for device '{template_item.device_name}'."
        )

    device_os_family = cfg.os_family or "linux"
    if template_item.category == "windows_common" and device_os_family != "windows":
        raise ValueError(
            f"TEMPLATE_NOT_APPLICABLE: Template '{command_key}' requires windows device, but '{device_name}' is '{device_os_family}'."
        )
    if template_item.category == "linux_common" and device_os_family != "linux":
        raise ValueError(
            f"TEMPLATE_NOT_APPLICABLE: Template '{command_key}' requires linux device, but '{device_name}' is '{device_os_family}'."
        )

    safe_args = sanitize_args(args or [])
    try:
        command = template_item.template.format(*safe_args)
    except IndexError as exc:
        raise ValueError("INVALID_COMMAND_ARGS: Command args do not match template placeholders.") from exc

    _guard_command_safety(command, cfg)

    with SshDeviceClient(cfg, use_pool=True) as client:
        result = client.exec(command, timeout_sec=timeout_sec)

    payload: dict[str, Any] = {
        "device": device_name,
        "command_key": command_key,
        "exit_code": result.exit_code,
        "elapsed_ms": result.elapsed_ms,
    }
    _audit("cmd.exec", payload)

    return {
        **payload,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


@mcp.tool()
def file_upload(device_name: str, local_path: str, remote_path: str) -> dict[str, Any]:
    """上传本地文件到远端路径（受 allowed_roots 限制）。

    何时使用：
    - 需要将构建产物或配置文件下发到设备时。
    """
    cfg = _get_device(device_name)
    lp = Path(local_path)
    if not lp.exists() or not lp.is_file():
        raise ValueError(f"LOCAL_FILE_NOT_FOUND: Local file not found: {local_path}")

    _guard_remote_path(cfg, remote_path)

    with SshDeviceClient(cfg, use_pool=True) as client:
        client.upload(str(lp), remote_path)

    payload: dict[str, Any] = {"device": device_name, "local_path": str(lp), "remote_path": remote_path}
    _audit("file.upload", payload)
    return {"ok": True, **payload}


@mcp.tool()
def file_download(device_name: str, remote_path: str, local_path: str) -> dict[str, Any]:
    """下载远端文件到本地路径（受 allowed_roots 限制）。

    何时使用：
    - 需要回收日志、配置或调试产物到本地时。
    """
    cfg = _get_device(device_name)

    _guard_remote_path(cfg, remote_path)

    lp = Path(local_path)
    if lp.parent:
        lp.parent.mkdir(parents=True, exist_ok=True)

    with SshDeviceClient(cfg, use_pool=True) as client:
        client.download(remote_path, str(lp))

    payload: dict[str, Any] = {"device": device_name, "remote_path": remote_path, "local_path": str(lp)}
    _audit("file.download", payload)
    return {"ok": True, **payload}


@mcp.tool()
def dir_list(device_name: str, remote_path: str) -> dict[str, Any]:
    """列出远端目录内容（文件名、类型、大小、修改时间、权限）。

    何时使用：
    - 需要查看设备目录结构时。
    - 确认文件是否存在及其大小时。
    """
    cfg = _get_device(device_name)

    _guard_remote_path(cfg, remote_path)

    with SshDeviceClient(cfg, use_pool=True) as client:
        entries = client.listdir(remote_path)

    payload: dict[str, Any] = {"device": device_name, "remote_path": remote_path, "count": len(entries)}
    _audit("dir.list", payload)
    return {"device": device_name, "path": remote_path, "count": len(entries), "entries": entries}


@mcp.tool()
def file_stat(device_name: str, remote_path: str) -> dict[str, Any]:
    """查询远端文件或目录的元信息（类型、大小、修改时间、权限）。

    何时使用：
    - 需要确认文件是否存在及其属性时。
    - 下载前验证文件大小时。
    """
    cfg = _get_device(device_name)

    _guard_remote_path(cfg, remote_path)

    with SshDeviceClient(cfg, use_pool=True) as client:
        info = client.stat(remote_path)

    _audit("file.stat", {"device": device_name, "remote_path": remote_path})
    return {"device": device_name, **info}


@mcp.tool()
def cmd_exec_batch(
    device_name: str,
    commands: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """并发执行多条命令模板，一次返回全部结果（顺序与输入一致）。

    commands 格式：
      [{"command_key": "...", "args": [...], "timeout_sec": 30}, ...]

    何时使用：
    - 所有命令的 exec_mode == "sync"，且它们互相无依赖时。
    - 需要同时采集多个指标（CPU、内存、磁盘、网络等）时。

    何时不用：
    - commands 中包含 exec_mode == "async" 的模板 → 每条改用 cmd_exec_async 单独提交。
    - 命令之间存在依赖顺序 → 改用多次 cmd_exec 串行调用。
    """
    cfg = _get_device(device_name)
    app_config = _get_app_config()

    # 预先校验并构建命令字符串列表
    prepared: list[tuple[str, str, int]] = []  # (command_key, command_str, timeout_sec)
    device_os_family = cfg.os_family or "linux"
    for item in commands:
        command_key = str(item.get("command_key", ""))
        raw_args: Any = item.get("args") or []
        timeout_sec = int(item.get("timeout_sec", 30))

        template_item = app_config.command_templates.get(command_key)
        if not template_item:
            raise ValueError(f"UNKNOWN_COMMAND_TEMPLATE: '{command_key}'")

        if template_item.category == "device_specific" and template_item.device_name != device_name:
            raise ValueError(
                f"TEMPLATE_NOT_APPLICABLE: Template '{command_key}' is only for device '{template_item.device_name}'."
            )
        if template_item.category == "windows_common" and device_os_family != "windows":
            raise ValueError(
                f"TEMPLATE_NOT_APPLICABLE: Template '{command_key}' requires windows device, but '{device_name}' is '{device_os_family}'."
            )
        if template_item.category == "linux_common" and device_os_family != "linux":
            raise ValueError(
                f"TEMPLATE_NOT_APPLICABLE: Template '{command_key}' requires linux device, but '{device_name}' is '{device_os_family}'."
            )

        safe_args = sanitize_args(list(raw_args))
        try:
            command_str = template_item.template.format(*safe_args)
        except IndexError as exc:
            raise ValueError(f"INVALID_COMMAND_ARGS for '{command_key}'") from exc

        _guard_command_safety(command_str, cfg)

        prepared.append((command_key, command_str, timeout_sec))

    def _run_one(label: str, cmd: str, timeout: int) -> dict[str, Any]:
        try:
            with SshDeviceClient(cfg, use_pool=True) as client:
                result = client.exec(cmd, timeout_sec=timeout)
                return {
                    "command_key": label,
                    "exit_code": result.exit_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "elapsed_ms": result.elapsed_ms,
                }
        except _EXEC_OPERATION_ERRORS as exc:
            return {"command_key": label, "error": str(exc)}

    results: list[dict[str, Any]] = [{}] * len(prepared)
    max_workers = min(len(prepared), 8)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {executor.submit(_run_one, *p): i for i, p in enumerate(prepared)}
        for future in as_completed(future_to_idx):
            results[future_to_idx[future]] = future.result()

    _audit("cmd.exec.batch", {"device": device_name, "count": len(prepared)})
    return results


def _jobs_cleanup_if_needed() -> None:
    """当任务数超过上限时，清理最旧的已完成任务（在 _JOBS_LOCK 内调用）。"""
    if len(_JOBS) < _JOBS_MAX:
        return
    done_keys = [
        k for k, v in _JOBS.items() if v.get("status") in ("done", "error")
    ]
    for k in done_keys[: len(done_keys) // 2 + 1]:
        _JOBS.pop(k, None)


def _reconfigure_stream_utf8(stream: Any) -> None:
    """尽量将文本流切换为 UTF-8，失败时保持原状。"""
    if not stream or not hasattr(stream, "reconfigure"):
        return

    try:
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (LookupError, OSError, ValueError):
        return


@mcp.tool()
def cmd_exec_async(
    device_name: str,
    command_key: str,
    args: list[str] | None = None,
    timeout_sec: int = 30,
) -> dict[str, Any]:
    """异步提交命令，立即返回 job_id，不阻塞等待结果。

    执行流程：
      1. cmd_exec_async(...)  → 返回 {job_id, status: "running"}
      2. cmd_exec_result(job_id) → status=="running" 时稍后重试
      3. cmd_exec_result(job_id) → status=="done" 时读取 stdout/stderr/exit_code

    何时使用：
    - 模板的 exec_mode == "async" 时必须使用本工具。
    - 命令耗时较长（固件烧录、编译、nohup 重启等），不应占用 Agent 主流程。
    - 需要同时提交多个独立长任务时（各自得到 job_id，并行等待）。

    何时不用：
    - 模板的 exec_mode == "sync" → 直接用 cmd_exec（无需 job_id 轮询开销）。
    """
    cfg = _get_device(device_name)
    app_config = _get_app_config()

    template_item = app_config.command_templates.get(command_key)
    if not template_item:
        raise ValueError(f"UNKNOWN_COMMAND_TEMPLATE: '{command_key}'")

    if template_item.category == "device_specific" and template_item.device_name != device_name:
        raise ValueError(
            f"TEMPLATE_NOT_APPLICABLE: Template '{command_key}' is only for device '{template_item.device_name}'."
        )
    device_os_family = cfg.os_family or "linux"
    if template_item.category == "windows_common" and device_os_family != "windows":
        raise ValueError(
            f"TEMPLATE_NOT_APPLICABLE: Template '{command_key}' requires windows device, but '{device_name}' is '{device_os_family}'."
        )
    if template_item.category == "linux_common" and device_os_family != "linux":
        raise ValueError(
            f"TEMPLATE_NOT_APPLICABLE: Template '{command_key}' requires linux device, but '{device_name}' is '{device_os_family}'."
        )

    safe_args = sanitize_args(args or [])
    try:
        command = template_item.template.format(*safe_args)
    except IndexError as exc:
        raise ValueError("INVALID_COMMAND_ARGS: Command args do not match template placeholders.") from exc

    _guard_command_safety(command, cfg)

    job_id = uuid.uuid4().hex
    submitted_at = datetime.now(timezone.utc).isoformat()

    with _JOBS_LOCK:
        _jobs_cleanup_if_needed()
        _JOBS[job_id] = {
            "status": "running",
            "device": device_name,
            "command_key": command_key,
            "submitted_at": submitted_at,
        }

    def _worker() -> None:
        update: dict[str, Any]
        try:
            with SshDeviceClient(cfg, use_pool=True) as client:
                result = client.exec(command, timeout_sec=timeout_sec)
            update = {
                "status": "done",
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "elapsed_ms": result.elapsed_ms,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        except _EXEC_OPERATION_ERRORS as exc:
            update = {
                "status": "error",
                "error": str(exc),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        with _JOBS_LOCK:
            if job_id in _JOBS:
                _JOBS[job_id].update(update)

    t = Thread(target=_worker, name=f"async-exec-{job_id[:8]}", daemon=True)
    t.start()

    _audit("cmd.exec.async", {"device": device_name, "command_key": command_key, "job_id": job_id})
    return {"job_id": job_id, "status": "running", "submitted_at": submitted_at}


@mcp.tool()
def cmd_exec_result(job_id: str) -> dict[str, Any]:
    """取回异步命令执行结果。

    status 字段说明：
    - "running"：命令仍在执行，建议等待 1-3 秒后重试。
    - "done"：执行完成，返回值包含 exit_code / stdout / stderr / elapsed_ms。
    - "error"：执行异常（SSH 断连等），返回值包含 error 字段。

    何时使用：
    - 调用 cmd_exec_async 后，用返回的 job_id 轮询直到 status != "running"。
    - 拿到 status=="done" 后读取 exit_code 判断命令是否成功（0 = 成功）。
    """
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise ValueError(f"UNKNOWN_JOB: Unknown job_id '{job_id}'.")
        # 在锁内复制，避免 worker 线程 update() 与此处并发产生撕裂读
        return dict(job)


def main() -> None:
    # Windows 下强制 stderr/stdout 使用 UTF-8，避免 VS Code 输出面板乱码。
    _reconfigure_stream_utf8(sys.stderr)
    _reconfigure_stream_utf8(sys.stdout)

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--audit", dest="audit_log")
    parser.add_argument("--transport", dest="transport")
    args, _ = parser.parse_known_args()

    if args.config_path:
        os.environ["MCP_DEVICE_CONFIG"] = args.config_path
    if args.audit_log:
        os.environ["MCP_AUDIT_LOG"] = args.audit_log
    if args.transport:
        os.environ["MCP_TRANSPORT"] = args.transport

    with _CONFIG_LOCK:
        _CONFIG_STATE["cache"] = None
        _CONFIG_STATE["fingerprint"] = None
        _CONFIG_STATE["last_error"] = None
        _CONFIG_STATE["available"] = False

    # 启动前预热配置状态：配置不可用时不退出，进入监测等待状态。
    _refresh_config_state(force=True)

    monitor = Thread(target=_config_monitor_loop, name="config-monitor", daemon=True)
    monitor.start()

    transport_raw = os.getenv("MCP_TRANSPORT", "stdio")
    transport_value: Literal["stdio", "sse", "streamable-http"]
    if transport_raw == "stdio":
        transport_value = "stdio"
    elif transport_raw == "sse":
        transport_value = "sse"
    elif transport_raw == "streamable-http":
        transport_value = "streamable-http"
    else:
        print(f"[WARN] Invalid MCP_TRANSPORT '{transport_raw}', fallback to 'stdio'.", file=sys.stderr)
        transport_value = "stdio"

    # 默认使用 stdio 传输，以获得更好的 MCP 客户端兼容性。
    mcp.run(transport=transport_value)


if __name__ == "__main__":
    main()
