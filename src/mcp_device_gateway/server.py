from __future__ import annotations

import argparse
import json
import os
import sys
import time
import re
from threading import Lock
from threading import Thread
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
import yaml

from .config import AppConfig, CommandTemplate, DeviceConfig, load_config
from .security import is_path_allowed, sanitize_args
from .ssh_client import SshDeviceClient

mcp = FastMCP("embedded-device-gateway")
_CONFIG_LOCK = Lock()
_CONFIG_STATE: dict[str, Any] = {
    "cache": None,
    "fingerprint": None,
    "last_error": None,
    "available": False,
}

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
    log_path = Path(_get_app_config().audit_log)
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
        "description": cfg.description,
        "when_to_use": cfg.when_to_use,
        "capabilities": list(cfg.capabilities),
        "tags": list(cfg.tags),
        "preferred_templates": list(cfg.preferred_templates),
    }


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
    client = SshDeviceClient(cfg)
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
        "workflow": [
            "先调用 device_list 获取设备",
            "必要时调用 device_profile_get 查看设备画像与推荐模板",
            "调用 device_ping 验证连通性",
            "调用 command_template_list 或 command_template_get 选择命令",
            "如只知道自然语言任务，可先调用 task_recommend 生成调用草案",
            "调用 cmd_exec 执行远端命令",
            "需要传输文件时使用 file_upload/file_download",
        ],
        "tools": [
            {"name": "device_list", "when": "获取设备清单"},
            {"name": "device_profile_get", "when": "查看设备能力画像"},
            {"name": "device_ping", "when": "执行前连通性检查"},
            {"name": "command_template_list", "when": "发现可执行命令模板"},
            {"name": "command_template_get", "when": "查看命令模板详情"},
            {"name": "task_recommend", "when": "根据自然语言任务生成工具调用建议"},
            {"name": "cmd_exec", "when": "执行命令模板"},
            {"name": "file_upload", "when": "上传本地文件到设备"},
            {"name": "file_download", "when": "下载设备文件到本地"},
        ],
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

        result: dict[str, Any] = {
            "task": task,
            "recommended_tool": "cmd_exec",
            "draft": {
                "device_name": suggested_device or "<device_name>",
                "command_key": best_key,
                "args": _guess_cmd_args(best_item),
                "timeout_sec": 30,
            },
            "matched_template": _serialize_template(best_key, best_item),
            "suggested_device": suggested_device or None,
            "reason": "根据模板关键词和用途说明匹配得到。",
            "next_steps": ["device_list", "device_profile_get", "device_ping", "command_template_get", "cmd_exec"],
        }
        _audit("task.recommend", {"tool": "cmd_exec", "command_key": best_key})
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
    """执行命令模板。

    何时使用：
    - 已通过 device_list/device_ping 确认目标设备。
    - 已通过 command_template_list 或 command_template_get 确认 command_key 与参数。
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

    with SshDeviceClient(cfg) as client:
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

    if cfg.allowed_roots and not is_path_allowed(remote_path, cfg.allowed_roots):
        raise ValueError(f"REMOTE_PATH_NOT_ALLOWED: Remote path is not allowed: {remote_path}")

    with SshDeviceClient(cfg) as client:
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

    if cfg.allowed_roots and not is_path_allowed(remote_path, cfg.allowed_roots):
        raise ValueError(f"REMOTE_PATH_NOT_ALLOWED: Remote path is not allowed: {remote_path}")

    lp = Path(local_path)
    if lp.parent:
        lp.parent.mkdir(parents=True, exist_ok=True)

    with SshDeviceClient(cfg) as client:
        client.download(remote_path, str(lp))

    payload: dict[str, Any] = {"device": device_name, "remote_path": remote_path, "local_path": str(lp)}
    _audit("file.download", payload)
    return {"ok": True, **payload}


def main() -> None:
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
