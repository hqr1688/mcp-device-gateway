from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from pathlib import Path, PurePosixPath
from threading import Lock, Thread
from typing import Any, Literal, cast

from mcp.server.fastmcp import FastMCP
import paramiko
import yaml

from .config import AppConfig, CommandTemplate, DeviceConfig, load_config
from .security import is_path_allowed, is_path_denied, match_dangerous_command, match_sensitive_path, scan_command_for_sensitive_paths, sanitize_args
from .ssh_client import SshDeviceClient, _pool

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
        should_close_pool = (
            old_fingerprint is not None
            and old_fingerprint != fingerprint
            and new_available
        )

    if generated_message:
        print(f"[WARN] {generated_message}", file=sys.stderr)

    if should_close_pool:
        _pool.close_all()
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
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def _get_device(device_name: str) -> DeviceConfig:
    app_config = _get_app_config()
    try:
        return app_config.devices[device_name]
    except KeyError as exc:
        raise ValueError(f"GW-3001 DEVICE_NOT_FOUND: Unknown device '{device_name}'.") from exc


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
        "arg_schema": [dict(schema) for schema in item.arg_schema],
        "success_on_exit_codes": list(item.success_on_exit_codes),
        "requires_privilege": item.requires_privilege,
        "capability_tags": list(item.capability_tags),
        "fallback_templates": list(item.fallback_templates),
        "parser": item.parser or None,
    }


def _serialize_device_profile(cfg: DeviceConfig) -> dict[str, Any]:
    return {
        "name": cfg.name,
        "os_family": cfg.os_family,
        "os_name": cfg.os_name,
        "os_version": cfg.os_version,
        "description": cfg.description,
        "when_to_use": cfg.when_to_use,
        "capabilities": list(cfg.capabilities),
        "tags": list(cfg.tags),
        "preferred_templates": list(cfg.preferred_templates),
    }


def _serialize_device_list_item(cfg: DeviceConfig) -> dict[str, Any]:
    return {
        "name": cfg.name,
        "host": cfg.host,
        "port": cfg.port,
        **_serialize_device_profile(cfg),
    }


def _guard_remote_path(cfg: DeviceConfig, remote_path: str) -> None:
    # 内置敏感路径始终拦截，优先级最高，不受任何配置覆盖。
    sensitive_reason = match_sensitive_path(remote_path, cfg.os_family)
    if sensitive_reason:
        raise ValueError(f"SENSITIVE_PATH_BLOCKED: {sensitive_reason}。")

    if cfg.denied_paths and is_path_denied(remote_path, cfg.denied_paths, cfg.os_family):
        raise ValueError(f"REMOTE_PATH_DENIED: Remote path is denied: {remote_path}")

    # 空 allowed_roots 表示不限制路径，此时仅应用 denied_paths 黑名单。
    if cfg.allowed_roots and not is_path_allowed(remote_path, cfg.allowed_roots, cfg.os_family):
        raise ValueError(f"REMOTE_PATH_NOT_ALLOWED: Remote path is not allowed: {remote_path}")


def _remote_path_must_be_absolute(remote_path: str, os_family: str) -> None:
    normalized = str(PurePosixPath(str(remote_path).replace("\\", "/").strip()))
    normalized_os = (os_family or "linux").strip().lower() or "linux"
    is_absolute = normalized.startswith("/")
    if normalized_os == "windows":
        is_absolute = re.match(r"^[a-zA-Z]:($|/)", normalized) is not None
    if not is_absolute:
        raise ValueError(
            f"REMOTE_PATH_MUST_BE_ABSOLUTE: Remote path must be absolute: {remote_path}"
        )


def _local_allowed_roots() -> tuple[Path, ...]:
    raw = os.getenv("MCP_LOCAL_ALLOWED_ROOTS", "").strip()
    if not raw:
        return ()
    roots: list[Path] = []
    for item in raw.split(os.pathsep):
        text = item.strip()
        if text:
            roots.append(Path(text).resolve())
    return tuple(roots)


def _guard_local_path(local_path: Path) -> Path:
    resolved = local_path.resolve()
    allowed_roots = _local_allowed_roots()
    if not allowed_roots:
        return resolved
    for root in allowed_roots:
        if resolved == root or root in resolved.parents:
            return resolved
    raise ValueError(f"LOCAL_PATH_NOT_ALLOWED: Local path is not allowed: {resolved}")


def _guard_command_safety(command: str, cfg: DeviceConfig) -> None:
    reason = match_dangerous_command(
        command,
        cfg.os_family,
        allow_kernel_module_ops=cfg.allow_kernel_module_ops,
    )
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


def _template_summary(key: str, item: CommandTemplate) -> dict[str, Any]:
    return {
        "key": key,
        "short_key": item.short_key,
        "category": item.category,
        "device_name": item.device_name,
        "exec_mode": item.exec_mode,
        "requires_privilege": item.requires_privilege,
        "fallback_templates": list(item.fallback_templates),
        "arg_count": _template_arg_count(item.template),
        "args": list(item.args),
        "arg_schema": [dict(schema) for schema in item.arg_schema],
        "examples": [list(e) for e in item.examples],
    }


def _coerce_bool(value: str) -> bool | None:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


def _validate_path_arg(value: str, schema: dict[str, Any]) -> str:
    allow_utf8 = bool(schema.get("allow_utf8", True))
    allow_space = bool(schema.get("allow_space", True))
    # 路径类参数允许 UTF-8 和空格，但仍阻断命令拼接相关危险字符。
    dangerous_chars = {"\n", "\r", "\t", ";", "|", "&", "`", "$", "<", ">", "\"", "'"}
    if any(ch in value for ch in dangerous_chars):
        raise ValueError(f"Path argument contains dangerous shell characters: {value}")
    if not allow_space and " " in value:
        raise ValueError(f"Path argument does not allow spaces: {value}")
    if not allow_utf8 and any(ord(ch) > 127 for ch in value):
        raise ValueError(f"Path argument does not allow UTF-8 characters: {value}")
    normalized = str(PurePosixPath(value))
    if not normalized or normalized == ".":
        raise ValueError(f"Path argument is invalid: {value}")
    return value


def _quote_path_arg(value: str, os_family: str) -> str:
    if " " not in value and "\t" not in value:
        return value
    normalized_os = (os_family or "linux").strip().lower() or "linux"
    if normalized_os == "windows":
        return f'"{value}"'
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _placeholder_has_surrounding_quotes(template: str, index: int) -> bool:
    placeholder = f"{{{index}}}"
    pos = template.find(placeholder)
    if pos <= 0:
        return False
    end = pos + len(placeholder)
    if end >= len(template):
        return False
    return template[pos - 1] == template[end] and template[pos - 1] in {'"', "'"}


def _validate_typed_args(template_item: CommandTemplate, args: list[str]) -> list[str]:
    if not template_item.arg_schema:
        return sanitize_args(args)

    sanitized: list[str] = []
    for index, raw in enumerate(args):
        schema = dict(template_item.arg_schema[index]) if index < len(template_item.arg_schema) else {}
        arg_type = str(schema.get("type", "")).strip().lower()
        value = str(raw)

        if arg_type == "path":
            sanitized.append(_validate_path_arg(value, schema))
            continue

        if arg_type == "int":
            try:
                int_value = int(value)
            except ValueError as exc:
                raise ValueError(f"Argument at index {index} must be int, got '{value}'.") from exc
            min_value = schema.get("min")
            max_value = schema.get("max")
            if min_value is not None and int_value < int(min_value):
                raise ValueError(f"Argument at index {index} must be >= {int(min_value)}.")
            if max_value is not None and int_value > int(max_value):
                raise ValueError(f"Argument at index {index} must be <= {int(max_value)}.")
            sanitized.append(str(int_value))
            continue

        if arg_type == "enum":
            options = [str(item) for item in cast(list[Any], schema.get("enum_values", []))]
            if options and value not in options:
                raise ValueError(f"Argument at index {index} must be one of {options}, got '{value}'.")
            sanitized.append(value)
            continue

        if arg_type == "bool":
            bool_value = _coerce_bool(value)
            if bool_value is None:
                raise ValueError(f"Argument at index {index} must be bool-like, got '{value}'.")
            sanitized.append("true" if bool_value else "false")
            continue

        if arg_type == "service_name":
            if not re.fullmatch(r"^[a-zA-Z0-9_.@-]+$", value):
                raise ValueError(f"Invalid service_name argument at index {index}: '{value}'.")
            sanitized.append(value)
            continue

        if arg_type == "filename":
            if "/" in value or "\\" in value:
                raise ValueError(f"filename argument must not contain path separators: '{value}'.")
            if any(ch in value for ch in ["\n", "\r", "\t", ";", "|", "&", "`", "$", "<", ">"]):
                raise ValueError(f"filename argument contains dangerous characters: '{value}'.")
            sanitized.append(value)
            continue

        # schema 存在但类型未识别时，回退到旧规则，保持兼容。
        sanitize_args([value])
        sanitized.append(value)

    return sanitized


def _permission_suggestion(template_item: CommandTemplate) -> str:
    if template_item.requires_privilege == "none":
        return ""
    fallback = ", ".join(template_item.fallback_templates)
    if fallback:
        return f"需要权限级别: {template_item.requires_privilege}。可尝试替代模板: {fallback}。"
    return f"需要权限级别: {template_item.requires_privilege}。"


def _validate_timeout_sec(timeout_sec: int) -> int:
    if timeout_sec < 1 or timeout_sec > 3600:
        raise ValueError(
            "GW-1006 INVALID_TIMEOUT: timeout_sec must be between 1 and 3600."
        )
    return timeout_sec


def _max_custom_command_len() -> int:
    raw = os.getenv("MCP_MAX_CUSTOM_COMMAND_LEN", "4096")
    try:
        return max(128, int(raw))
    except ValueError:
        return 4096


def _validate_custom_command(command: str) -> str:
    text = command.strip()
    if not text:
        raise ValueError("GW-1007 INVALID_CUSTOM_COMMAND: command must not be empty.")
    if any(ch in text for ch in ("\r", "\n", "\x00")):
        raise ValueError("GW-1007 INVALID_CUSTOM_COMMAND: command must be a single-line text.")
    max_len = _max_custom_command_len()
    if len(text) > max_len:
        raise ValueError(
            f"GW-1007 INVALID_CUSTOM_COMMAND: command is too long ({len(text)} > {max_len})."
        )
    return text


def _async_job_ttl_sec() -> int:
    raw = os.getenv("MCP_ASYNC_JOB_TTL_SEC", "1800")
    try:
        return max(1, int(raw))
    except ValueError:
        return 1800


def _normalize_exec_status(template_item: CommandTemplate, exit_code: int, stdout: str, stderr: str, elapsed_ms: int, timeout_sec: int) -> dict[str, Any]:
    success_codes = tuple(template_item.success_on_exit_codes) or (0,)
    has_output = bool(stdout.strip() or stderr.strip())
    normalized_status = "failed"
    if exit_code in success_codes:
        normalized_status = "success" if exit_code == 0 else "partial"
    elif has_output and exit_code == 124:
        normalized_status = "partial"

    return {
        "normalized_status": normalized_status,
        "success_on_exit_codes": list(success_codes),
        "collected_duration_ms": min(elapsed_ms, max(0, timeout_sec) * 1000),
        "output_truncated": bool(timeout_sec > 0 and elapsed_ms >= timeout_sec * 1000 and exit_code != 0),
    }


def _normalize_custom_exec_status(exit_code: int, stdout: str, stderr: str, elapsed_ms: int, timeout_sec: int) -> dict[str, Any]:
    has_output = bool(stdout.strip() or stderr.strip())
    normalized_status = "success" if exit_code == 0 else "failed"
    if exit_code == 124 and has_output:
        normalized_status = "partial"
    return {
        "normalized_status": normalized_status,
        "success_on_exit_codes": [0],
        "collected_duration_ms": min(elapsed_ms, max(0, timeout_sec) * 1000),
        "output_truncated": bool(timeout_sec > 0 and elapsed_ms >= timeout_sec * 1000 and exit_code != 0),
    }


def _infer_parser(template_item: CommandTemplate) -> str:
    if template_item.parser:
        return template_item.parser
    tmpl = template_item.template.lower().strip()
    if tmpl.startswith("free"):
        return "free"
    if tmpl.startswith("df"):
        return "df"
    if "systemctl" in tmpl:
        return "systemctl"
    if "journalctl" in tmpl:
        return "journal"
    return ""


def _parse_structured_output(template_item: CommandTemplate, stdout: str) -> Any:
    parser_name = _infer_parser(template_item)
    text = stdout.strip()
    if not parser_name or not text:
        return None

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None

    try:
        if parser_name == "free":
            for line in lines:
                if line.lower().startswith("mem:"):
                    parts = line.split()
                    if len(parts) >= 7:
                        return {
                            "memory": {
                                "total": int(parts[1]),
                                "used": int(parts[2]),
                                "free": int(parts[3]),
                                "shared": int(parts[4]),
                                "buff_cache": int(parts[5]),
                                "available": int(parts[6]),
                            }
                        }
            return None

        if parser_name == "df":
            header = re.split(r"\s+", lines[0].strip())
            rows: list[dict[str, str]] = []
            for line in lines[1:]:
                cols = re.split(r"\s+", line.strip(), maxsplit=max(0, len(header) - 1))
                if len(cols) == len(header):
                    rows.append({header[i]: cols[i] for i in range(len(header))})
            return {"filesystems": rows}

        if parser_name == "systemctl":
            units: list[dict[str, str]] = []
            for line in lines:
                cols = re.split(r"\s+", line.strip(), maxsplit=4)
                if len(cols) >= 4 and ".service" in cols[0]:
                    units.append(
                        {
                            "unit": cols[0],
                            "load": cols[1],
                            "active": cols[2],
                            "sub": cols[3],
                            "description": cols[4] if len(cols) >= 5 else "",
                        }
                    )
            return {"services": units}

        if parser_name == "journal":
            return {"lines": lines}
    except (IndexError, ValueError):
        return None

    return None


def _template_candidates(app_config: AppConfig, command_key: str, limit: int = 5) -> list[str]:
    keys = sorted(app_config.command_templates.keys())
    if not keys:
        return []

    if command_key in keys:
        return [command_key]

    fuzzy = get_close_matches(command_key, keys, n=limit, cutoff=0.1)
    if fuzzy:
        return fuzzy

    # 回退到前缀/子串匹配，提升 UNKNOWN 场景下的可诊断性。
    lowered = command_key.lower()
    matched = [k for k in keys if lowered in k.lower() or k.lower().endswith(f".{lowered}")]
    return matched[:limit]


def _unknown_template_error(command_key: str, app_config: AppConfig) -> ValueError:
    candidates = _template_candidates(app_config, command_key)
    did_you_mean = f" Did you mean: {candidates[0]}" if candidates else ""
    return ValueError(
        f"GW-2001 UNKNOWN_COMMAND_TEMPLATE: Unknown command template '{command_key}'.{did_you_mean}"
    )


def _response_envelope(
    *,
    status: str,
    data: Any,
    request_id: str | None = None,
    error: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    compat_mode: str = "legacy",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "request_id": request_id or uuid.uuid4().hex,
        "timestamp": now,
        "api_version": "1.1",
        "status": status,
        "error": error,
        "data": data,
        "meta": {
            "compat_mode": compat_mode,
            **(meta or {}),
        },
    }


def _error_object_from_exception(exc: Exception) -> dict[str, Any]:
    text = str(exc).strip()
    match = re.match(r"^(GW-\d{4})\s+([A-Z0-9_]+):\s*(.*)$", text)
    if match:
        code = match.group(1)
        machine_code = match.group(2)
        message = match.group(3) or text
    else:
        legacy_map = {
            "UNKNOWN_COMMAND_TEMPLATE": "GW-2001",
            "INVALID_COMMAND_ARGS": "GW-1002",
            "DEVICE_NOT_FOUND": "GW-3001",
            "UNKNOWN_JOB": "GW-4002",
            "INVALID_CHANGED_SINCE": "GW-1003",
            "AMBIGUOUS_COMMAND_TEMPLATE": "GW-2002",
            "TEMPLATE_NOT_APPLICABLE": "GW-2003",
            "EXEC_MODE_MISMATCH": "GW-2004",
            "INVALID_PAGE": "GW-1004",
            "INVALID_PAGE_SIZE": "GW-1005",
            "INVALID_TIMEOUT": "GW-1006",
            "INVALID_CUSTOM_COMMAND": "GW-1007",
            "LOCAL_PATH_NOT_ALLOWED": "GW-3002",
            "REMOTE_PATH_MUST_BE_ABSOLUTE": "GW-3003",
            "JOB_EXPIRED": "GW-4003",
        }
        prefix_match = re.match(r"^([A-Z_]+):\s*(.*)$", text)
        if prefix_match and prefix_match.group(1) in legacy_map:
            machine_code = prefix_match.group(1)
            code = legacy_map[machine_code]
            message = prefix_match.group(2) or text
        else:
            code = "GW-5000"
            machine_code = "INTERNAL_ERROR"
            message = text or "Internal error"

    suggestion = ""
    if "Did you mean:" in message:
        suggestion = message.split("Did you mean:", maxsplit=1)[1].strip()
    recoverable = not code.startswith("GW-5")
    return {
        "code": code,
        "message": message,
        "details": {
            "machine_code": machine_code,
            "raw": text,
        },
        "recoverable": recoverable,
        "suggestion": suggestion or None,
    }


def _resolve_command_template(
    app_config: AppConfig,
    command_key: str,
    *,
    device_name: str,
    strict: bool,
) -> tuple[str, CommandTemplate, dict[str, Any]]:
    direct = app_config.command_templates.get(command_key)
    if direct is not None:
        return command_key, direct, {"mode": "exact_full_key", "candidates": []}

    if strict:
        raise _unknown_template_error(command_key, app_config)

    scoped_matches = [
        (k, item)
        for k, item in app_config.command_templates.items()
        if item.short_key == command_key and item.device_name == device_name
    ]
    if len(scoped_matches) == 1:
        matched_key, matched_item = scoped_matches[0]
        return matched_key, matched_item, {"mode": "short_key_scoped", "candidates": []}
    if len(scoped_matches) > 1:
        candidates = sorted(k for k, _ in scoped_matches)
        raise ValueError(
            f"GW-2002 AMBIGUOUS_COMMAND_TEMPLATE: Short key '{command_key}' has multiple matches in device scope '{device_name}': {candidates}."
        )

    global_matches = [
        (k, item)
        for k, item in app_config.command_templates.items()
        if item.short_key == command_key and item.device_name is None
    ]
    if len(global_matches) == 1:
        matched_key, matched_item = global_matches[0]
        return matched_key, matched_item, {"mode": "short_key_global", "candidates": []}
    if len(global_matches) > 1:
        candidates = sorted(k for k, _ in global_matches)
        raise ValueError(
            f"GW-2002 AMBIGUOUS_COMMAND_TEMPLATE: Short key '{command_key}' matches multiple templates: {candidates}."
        )

    alias_target = app_config.alias_registry.get(command_key)
    if alias_target:
        alias_item = app_config.command_templates.get(alias_target)
        if alias_item is not None:
            return alias_target, alias_item, {"mode": "alias", "candidates": []}

    raise _unknown_template_error(command_key, app_config)


def _validate_template_applicability(template_item: CommandTemplate, *, command_key: str, cfg: DeviceConfig, device_name: str) -> None:
    if template_item.category == "device_specific" and template_item.device_name != device_name:
        raise ValueError(
            f"GW-2003 TEMPLATE_NOT_APPLICABLE: Template '{command_key}' is only for device '{template_item.device_name}'."
        )

    device_os_family = cfg.os_family or "linux"
    if template_item.category == "windows_common" and device_os_family != "windows":
        raise ValueError(
            f"GW-2003 TEMPLATE_NOT_APPLICABLE: Template '{command_key}' requires windows device, but '{device_name}' is '{device_os_family}'."
        )
    if template_item.category == "linux_common" and device_os_family != "linux":
        raise ValueError(
            f"GW-2003 TEMPLATE_NOT_APPLICABLE: Template '{command_key}' requires linux device, but '{device_name}' is '{device_os_family}'."
        )


def _render_command(
    command_key: str,
    template_item: CommandTemplate,
    args: list[str] | None,
    *,
    os_family: str,
) -> str:
    safe_args = _validate_typed_args(template_item, args or [])
    rendered_args = list(safe_args)
    for index, value in enumerate(rendered_args):
        schema = dict(template_item.arg_schema[index]) if index < len(template_item.arg_schema) else {}
        arg_type = str(schema.get("type", "")).strip().lower()
        if arg_type != "path":
            continue
        if _placeholder_has_surrounding_quotes(template_item.template, index):
            continue
        rendered_args[index] = _quote_path_arg(value, os_family)
    try:
        return template_item.template.format(*rendered_args)
    except (IndexError, ValueError) as exc:
        expected_count = _template_arg_count(template_item.template)
        details: dict[str, Any] = {
            "command_key": command_key,
            "expected_args": expected_count,
            "received_args": len(rendered_args),
            "arg_names": list(template_item.args),
            "arg_schema": [dict(schema) for schema in template_item.arg_schema],
            "examples": [list(e) for e in template_item.examples],
            "template_summary": _template_summary(command_key, template_item),
        }
        raise ValueError(f"GW-1002 INVALID_COMMAND_ARGS: {json.dumps(details, ensure_ascii=False)}") from exc


_CAPABILITY_TOOLS: list[dict[str, str]] = [
    {"name": "capability_overview", "when": "首次接入时查看服务能力地图、自检结果与推荐工作流"},
    {"name": "device_list", "when": "获取设备清单"},
    {"name": "device_profile_get", "when": "查看设备能力画像"},
    {"name": "device_ping", "when": "执行前连通性检查"},
    {"name": "command_template_list", "when": "发现可执行命令模板"},
    {"name": "command_template_get", "when": "查看命令模板详情（含 exec_mode 字段）"},
    {"name": "task_recommend", "when": "根据自然语言任务生成工具调用建议（自动选择执行模式）"},
    {"name": "cmd_exec", "when": "统一执行入口：支持单条/多条命令，每条可选 sync/async"},
    {"name": "cmd_exec_result", "when": "取回 cmd_exec 异步任务的执行结果，status=done 时获得完整输出"},
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
        "5. 执行命令统一使用 cmd_exec：\n"
        "   - 单条命令：传 command_key（模板）或 command（自定义），并设置 mode=sync/async。\n"
        "   - 多条命令：传 commands 列表，每条可独立设置 mode=sync/async。\n"
        "6. 对于异步条目，用 cmd_exec_result(job_id) 轮询到 done/error。\n"
        "7. 文件传输场景使用 file_upload/file_download，并在返回中包含风险说明与下一步建议。\n\n"
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
    data: list[dict[str, Any]] = [_serialize_device_list_item(cfg) for cfg in app_config.devices.values()]
    _audit("device.list", {"count": len(data)})
    return data


@mcp.tool()
def device_profile_get(device_name: str) -> dict[str, Any]:
    """获取单个设备画像（用途、能力、推荐模板）。

    何时使用：
    - 已知设备名，想确认该设备最适合执行什么任务时。
    """
    cfg = _get_device(device_name)
    data = _serialize_device_profile(cfg)
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
def command_template_list(
    device_name: str | None = None,
    category: str | None = None,
    exec_mode: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 100,
    only_fields: list[str] | None = None,
    changed_since: str | None = None,
    compat_mode: Literal["legacy", "v1_1"] = "legacy",
) -> dict[str, Any]:
    """列出所有命令模板及用途说明。

    何时使用：
    - 不确定 command_key 时优先调用。
    - 需要确认参数数量、风险级别与适用场景时调用。
    """
    try:
        app_config = _get_app_config()
        templates: list[dict[str, Any]] = [_serialize_template(k, v) for k, v in app_config.command_templates.items()]
        if page < 1:
            raise ValueError("GW-1004 INVALID_PAGE: page must be >= 1.")
        if page_size < 1 or page_size > 500:
            raise ValueError("GW-1005 INVALID_PAGE_SIZE: page_size must be between 1 and 500.")

        if changed_since:
            try:
                changed_since_dt = datetime.fromisoformat(changed_since.replace("Z", "+00:00"))
                changed_since_ns = int(changed_since_dt.timestamp() * 1_000_000_000)
            except ValueError as exc:
                raise ValueError(f"GW-1003 INVALID_CHANGED_SINCE: Invalid changed_since '{changed_since}'.") from exc

            with _CONFIG_LOCK:
                fingerprint = cast(tuple[Any, ...] | None, _CONFIG_STATE.get("fingerprint"))
            config_mtime_ns = 0
            if fingerprint is not None and len(fingerprint) >= 3:
                config_mtime_ns = int(fingerprint[2])

            if config_mtime_ns and config_mtime_ns <= changed_since_ns:
                legacy_data: dict[str, Any] = {
                    "count": 0,
                    "total": 0,
                    "page": page,
                    "page_size": page_size,
                    "templates": [],
                    "changed": False,
                    "args_sanitize_pattern": "^[a-zA-Z0-9_./:\\=+\\-]+$",
                    "key_resolution": {
                        "accepted_in_exec": ["full_key", "short_key_when_strict_false", "alias"],
                        "default_strict": False,
                    },
                    "alias_registry": dict(app_config.alias_registry),
                }
                if compat_mode == "v1_1":
                    return _response_envelope(status="success", data=legacy_data, compat_mode=compat_mode)
                return legacy_data

        if device_name:
            device_name_norm = device_name.strip()
            templates = [item for item in templates if item.get("device_name") in (None, device_name_norm)]

        if category:
            category_norm = category.strip().lower()
            templates = [item for item in templates if str(item.get("category", "")).lower() == category_norm]

        if exec_mode:
            exec_mode_norm = exec_mode.strip().lower()
            templates = [item for item in templates if str(item.get("exec_mode", "")).lower() == exec_mode_norm]

        if keyword:
            keyword_norm = keyword.strip().lower()
            templates = [
                item
                for item in templates
                if keyword_norm in json.dumps(item, ensure_ascii=False).lower()
            ]

        templates = sorted(templates, key=lambda item: str(item.get("key", "")))
        total = len(templates)
        start = (page - 1) * page_size
        end = start + page_size
        templates = templates[start:end]

        if only_fields:
            field_set = {str(field).strip() for field in only_fields if str(field).strip()}
            field_set.add("key")
            templates = [{k: v for k, v in item.items() if k in field_set} for item in templates]

        result: dict[str, Any] = {
            "count": len(templates),
            "total": total,
            "page": page,
            "page_size": page_size,
            "templates": templates,
            "changed": True,
            "args_sanitize_pattern": "^[a-zA-Z0-9_./:\\=+\\-]+$",
            "key_resolution": {
                "accepted_in_exec": ["full_key", "short_key_when_strict_false", "alias"],
                "default_strict": False,
            },
            "alias_registry": dict(app_config.alias_registry),
            "recommended_workflow": [
                "device_list",
                "device_ping",
                "command_template_list",
                "cmd_exec",
            ],
        }
        _audit("command_template.list", {"count": len(templates), "total": total})
        if compat_mode == "v1_1":
            return _response_envelope(status="success", data=result, compat_mode=compat_mode)
        return result
    except ValueError as exc:
        if compat_mode == "v1_1":
            return _response_envelope(
                status="failed",
                data=None,
                error=_error_object_from_exception(exc),
                compat_mode=compat_mode,
                meta={"tool": "command_template_list"},
            )
        raise


@mcp.tool()
def command_template_get(command_key: str, compat_mode: Literal["legacy", "v1_1"] = "legacy") -> dict[str, Any]:
    """获取单个命令模板详情。

    何时使用：
    - 已确定 command_key，需要查看参数与风险信息时调用。
    """
    try:
        app_config = _get_app_config()
        item = app_config.command_templates.get(command_key)
        if item is None:
            raise _unknown_template_error(command_key, app_config)

        data: dict[str, Any] = _serialize_template(command_key, item)
        _audit("command_template.get", {"command_key": command_key})
        if compat_mode == "v1_1":
            return _response_envelope(status="success", data=data, compat_mode=compat_mode)
        return data
    except ValueError as exc:
        if compat_mode == "v1_1":
            return _response_envelope(
                status="failed",
                data=None,
                error=_error_object_from_exception(exc),
                compat_mode=compat_mode,
                meta={"tool": "command_template_get"},
            )
        raise


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
            "调用 cmd_exec 统一执行命令（支持单条/多条、每条 sync/async）",
            "异步条目提交后使用 cmd_exec_result 轮询结果",
            "需要浏览目录时使用 dir_list；查询单个文件属性使用 file_stat",
            "需要传输文件时使用 file_upload/file_download",
        ],
        "exec_mode_guide": {
            "decision_rule": "执行任何命令前，先调用 command_template_get 查看模板信息，然后在 cmd_exec 中为每条命令选择 mode。",
            "modes": [
                {
                    "mode": "sync",
                    "characteristics": "命令秒级完成，需立即取回结果",
                    "usage": "cmd_exec 中该条命令设置 mode=sync",
                    "typical_examples": ["查看进程", "读取日志最后几行", "health_check", "查磁盘"],
                },
                {
                    "mode": "async",
                    "characteristics": "命令耗时长（数秒至数分钟），不应阻塞 Agent 主流程",
                    "tool_sequence": ["cmd_exec（该条 mode=async）→ 立即得到 job_id", "cmd_exec_result(job_id) → 轮询直到 status!=running"],
                    "typical_examples": ["run_idm（nohup 重启进程）", "vm_make（编译）", "vm_deploy（scp 传输）"],
                },
            ],
            "shortcut": "如只有自然语言任务描述，调用 task_recommend —— 它会在 draft 中给出 mode 建议。",
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

    exact_input = re.sub(r"\s+", "", lowered)
    if exact_input:
        if exact_input in app_config.alias_registry:
            mapped_key = app_config.alias_registry[exact_input]
            mapped_item = app_config.command_templates.get(mapped_key)
            if mapped_item is not None:
                recommended_tool = "cmd_exec"
                return {
                    "task": task,
                    "recommended_tool": recommended_tool,
                    "draft": {
                        "device_name": mapped_item.device_name or "<device_name>",
                        "command_key": mapped_key,
                        "args": _guess_cmd_args(mapped_item),
                        "mode": mapped_item.exec_mode,
                        "timeout_sec": 30,
                    },
                    "matched_template": _serialize_template(mapped_key, mapped_item),
                    "reason": f"输入命中了 alias '{exact_input}'，已映射为 '{mapped_key}'。",
                    "next_steps": ["device_list", "command_template_get", recommended_tool],
                }

        direct_item = app_config.command_templates.get(exact_input)
        if direct_item is not None:
            recommended_tool = "cmd_exec"
            return {
                "task": task,
                "recommended_tool": recommended_tool,
                "draft": {
                    "device_name": direct_item.device_name or "<device_name>",
                    "command_key": exact_input,
                    "args": _guess_cmd_args(direct_item),
                    "mode": direct_item.exec_mode,
                    "timeout_sec": 30,
                },
                "matched_template": _serialize_template(exact_input, direct_item),
                "reason": "输入与模板 key 精确匹配。",
                "next_steps": ["device_list", "command_template_get", recommended_tool],
            }

        short_candidates = [
            (k, item)
            for k, item in app_config.command_templates.items()
            if item.short_key.lower() == exact_input
        ]
        if len(short_candidates) == 1:
            matched_key, matched_item = short_candidates[0]
            recommended_tool = "cmd_exec"
            return {
                "task": task,
                "recommended_tool": recommended_tool,
                "draft": {
                    "device_name": matched_item.device_name or "<device_name>",
                    "command_key": matched_key,
                    "args": _guess_cmd_args(matched_item),
                    "mode": matched_item.exec_mode,
                    "timeout_sec": 30,
                },
                "matched_template": _serialize_template(matched_key, matched_item),
                "reason": f"输入命中 short_key，已纠正为完整 key '{matched_key}'。",
                "next_steps": ["device_list", "command_template_get", recommended_tool],
            }

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

        recommended_tool = "cmd_exec"
        draft: dict[str, Any] = {
            "device_name": suggested_device or "<device_name>",
            "command_key": best_key,
            "args": _guess_cmd_args(best_item),
            "mode": best_item.exec_mode,
            "timeout_sec": 30,
        }
        next_steps = ["device_list", "device_profile_get", "device_ping", "command_template_get", recommended_tool]
        if best_item.exec_mode == "async":
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

    use_custom_async = any(word in lowered for word in ["编译", "部署", "构建", "升级", "下载固件", "压测", "stress", "build", "deploy"])
    custom_mode = "async" if use_custom_async else "sync"
    result = {
        "task": task,
        "recommended_tool": "cmd_exec",
        "draft": {
            "device_name": "<device_name>",
            "command": "<custom_command>",
            "mode": custom_mode,
            "timeout_sec": 30,
        },
        "reason": "未命中高置信度命令模板。默认优先模板；若模板不覆盖场景，建议使用受安全规则约束的自定义命令。",
        "next_steps": ["command_template_list", "command_template_get", "cmd_exec"],
    }
    _audit("task.recommend", {"tool": "cmd_exec", "fallback": "custom", "mode": custom_mode})
    return result


@mcp.tool()
def cmd_exec(
    device_name: str,
    command_key: str | None = None,
    args: list[str] | None = None,
    command: str | None = None,
    mode: Literal["sync", "async"] | None = None,
    timeout_sec: int = 30,
    commands: list[dict[str, Any]] | None = None,
    strict: bool = False,
    fail_fast: bool = False,
    compat_mode: Literal["legacy", "v1_1"] = "legacy",
) -> dict[str, Any]:
    """统一执行命令接口。

    支持两种调用形态：
    - 单条：传 command_key（模板）或 command（自定义）
    - 多条：传 commands 列表；每条都可设置 mode=sync/async
    """
    try:
        cfg = _get_device(device_name)
        app_config = _get_app_config()

        if commands is not None and (
            command_key is not None
            or command is not None
            or args is not None
            or mode is not None
        ):
            raise ValueError(
                "GW-1002 INVALID_COMMAND_ARGS: When commands is provided, do not pass command_key/command/args/mode."
            )

        single_call = commands is None

        if single_call:
            default_timeout = _validate_timeout_sec(timeout_sec)
            items: list[dict[str, Any]] = [
                {
                    "command_key": command_key,
                    "command": command,
                    "args": args or [],
                    "mode": mode,
                    "timeout_sec": default_timeout,
                    "strict": strict,
                }
            ]
        else:
            items = commands

        prepared: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []

        for index, item in enumerate(items):
            try:
                item_strict = bool(item.get("strict", strict))
                item_timeout = _validate_timeout_sec(int(item.get("timeout_sec", timeout_sec)))
                raw_mode = str(item.get("mode") or "").strip().lower()
                if raw_mode and raw_mode not in {"sync", "async"}:
                    raise ValueError(f"GW-1002 INVALID_COMMAND_ARGS: mode must be sync/async, got '{raw_mode}'.")

                key_value = item.get("command_key")
                command_key_text = key_value.strip() if isinstance(key_value, str) else ""
                command_value = item.get("command")
                command_text = command_value.strip() if isinstance(command_value, str) else ""

                if bool(command_key_text) == bool(command_text):
                    raise ValueError(
                        "GW-1002 INVALID_COMMAND_ARGS: Exactly one of command_key or command must be provided for each command item."
                    )

                if command_key_text:
                    resolved_key, template_item, resolution_meta = _resolve_command_template(
                        app_config,
                        command_key_text,
                        device_name=device_name,
                        strict=item_strict,
                    )
                    _validate_template_applicability(template_item, command_key=resolved_key, cfg=cfg, device_name=device_name)
                    raw_args: Any = item.get("args") or []
                    if not isinstance(raw_args, list):
                        raise ValueError(f"GW-1002 INVALID_COMMAND_ARGS: args for '{command_key_text}' must be a list.")
                    args_list = [str(arg) for arg in cast(list[Any], raw_args)]
                    rendered = _render_command(
                        resolved_key,
                        template_item,
                        args_list,
                        os_family=cfg.os_family,
                    )
                    _guard_command_safety(rendered, cfg)
                    prepared.append(
                        {
                            "index": index,
                            "mode": raw_mode or template_item.exec_mode,
                            "timeout_sec": item_timeout,
                            "kind": "template",
                            "input_key": command_key_text,
                            "resolved_key": resolved_key,
                            "resolution_mode": resolution_meta["mode"],
                            "strict": item_strict,
                            "command": rendered,
                            "template_item": template_item,
                        }
                    )
                else:
                    rendered = _validate_custom_command(command_text)
                    _guard_command_safety(rendered, cfg)
                    prepared.append(
                        {
                            "index": index,
                            "mode": raw_mode or "sync",
                            "timeout_sec": item_timeout,
                            "kind": "custom",
                            "command": rendered,
                        }
                    )
            except ValueError as exc:
                if fail_fast or single_call:
                    raise
                results.append(
                    {
                        "index": index,
                        "status": "failed",
                        "error": str(exc),
                    }
                )

        def _submit_async(spec: dict[str, Any]) -> dict[str, Any]:
            job_id = uuid.uuid4().hex
            submitted_at = datetime.now(timezone.utc).isoformat()
            with _JOBS_LOCK:
                _jobs_cleanup_if_needed()
                _JOBS[job_id] = {
                    "status": "running",
                    "device": device_name,
                    "submitted_at": submitted_at,
                    "mode": "async",
                    "kind": spec["kind"],
                    "command": spec["command"],
                }
                if spec["kind"] == "template":
                    _JOBS[job_id].update(
                        {
                            "command_key": spec["input_key"],
                            "resolved_key": spec["resolved_key"],
                            "resolution": {
                                "mode": spec["resolution_mode"],
                                "matched_key": spec["resolved_key"],
                                "strict": spec["strict"],
                            },
                        }
                    )

            def _worker() -> None:
                update: dict[str, Any]
                try:
                    with SshDeviceClient(cfg, use_pool=True) as client:
                        exec_result = client.exec(spec["command"], timeout_sec=int(spec["timeout_sec"]))
                    if spec["kind"] == "template":
                        template_item = cast(CommandTemplate, spec["template_item"])
                        normalized = _normalize_exec_status(
                            template_item,
                            exec_result.exit_code,
                            exec_result.stdout,
                            exec_result.stderr,
                            exec_result.elapsed_ms,
                            int(spec["timeout_sec"]),
                        )
                        structured_output = _parse_structured_output(template_item, exec_result.stdout)
                        permission_suggestion = ""
                        if "permission denied" in exec_result.stderr.lower():
                            permission_suggestion = _permission_suggestion(template_item)
                        update = {
                            "status": "done",
                            "exit_code": exec_result.exit_code,
                            "stdout": exec_result.stdout,
                            "stderr": exec_result.stderr,
                            "elapsed_ms": exec_result.elapsed_ms,
                            **normalized,
                            "structured_output": structured_output,
                            "raw_output": {
                                "stdout": exec_result.stdout,
                                "stderr": exec_result.stderr,
                            },
                            "requires_privilege": template_item.requires_privilege,
                            "fallback_templates": list(template_item.fallback_templates),
                            "permission_suggestion": permission_suggestion or None,
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                        }
                    else:
                        normalized = _normalize_custom_exec_status(
                            exec_result.exit_code,
                            exec_result.stdout,
                            exec_result.stderr,
                            exec_result.elapsed_ms,
                            int(spec["timeout_sec"]),
                        )
                        update = {
                            "status": "done",
                            "exit_code": exec_result.exit_code,
                            "stdout": exec_result.stdout,
                            "stderr": exec_result.stderr,
                            "elapsed_ms": exec_result.elapsed_ms,
                            **normalized,
                            "raw_output": {
                                "stdout": exec_result.stdout,
                                "stderr": exec_result.stderr,
                            },
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

            worker = Thread(target=_worker, name=f"cmd-exec-async-{job_id[:8]}", daemon=True)
            worker.start()

            response: dict[str, Any] = {
                "index": spec["index"],
                "mode": "async",
                "status": "running",
                "job_id": job_id,
                "submitted_at": submitted_at,
            }
            if spec["kind"] == "template":
                response.update(
                    {
                        "command_key": spec["input_key"],
                        "resolved_key": spec["resolved_key"],
                        "resolution": {
                            "mode": spec["resolution_mode"],
                            "matched_key": spec["resolved_key"],
                            "strict": spec["strict"],
                        },
                    }
                )
            else:
                response["command"] = spec["command"]
            return response

        for spec in prepared:
            item_mode = str(spec["mode"]).strip().lower() or "sync"
            if item_mode == "async":
                results.append(_submit_async(spec))
                continue

            try:
                with SshDeviceClient(cfg, use_pool=True) as client:
                    exec_result = client.exec(spec["command"], timeout_sec=int(spec["timeout_sec"]))
            except _EXEC_OPERATION_ERRORS as exc:
                if single_call or fail_fast:
                    raise
                results.append(
                    {
                        "index": spec["index"],
                        "mode": "sync",
                        "status": "error",
                        "error": str(exc),
                        "command_key": spec.get("input_key"),
                        "resolved_key": spec.get("resolved_key"),
                        "command": spec["command"],
                    }
                )
                continue

            if spec["kind"] == "template":
                template_item = cast(CommandTemplate, spec["template_item"])
                normalized = _normalize_exec_status(
                    template_item,
                    exec_result.exit_code,
                    exec_result.stdout,
                    exec_result.stderr,
                    exec_result.elapsed_ms,
                    int(spec["timeout_sec"]),
                )
                structured_output = _parse_structured_output(template_item, exec_result.stdout)
                permission_suggestion = ""
                if "permission denied" in exec_result.stderr.lower():
                    permission_suggestion = _permission_suggestion(template_item)
                results.append(
                    {
                        "index": spec["index"],
                        "mode": "sync",
                        "status": normalized["normalized_status"],
                        "command_key": spec["input_key"],
                        "resolved_key": spec["resolved_key"],
                        "exit_code": exec_result.exit_code,
                        "stdout": exec_result.stdout,
                        "stderr": exec_result.stderr,
                        "elapsed_ms": exec_result.elapsed_ms,
                        "resolution": {
                            "mode": spec["resolution_mode"],
                            "matched_key": spec["resolved_key"],
                            "strict": spec["strict"],
                        },
                        **normalized,
                        "structured_output": structured_output,
                        "raw_output": {
                            "stdout": exec_result.stdout,
                            "stderr": exec_result.stderr,
                        },
                        "requires_privilege": template_item.requires_privilege,
                        "fallback_templates": list(template_item.fallback_templates),
                        "permission_suggestion": permission_suggestion or None,
                    }
                )
            else:
                normalized = _normalize_custom_exec_status(
                    exec_result.exit_code,
                    exec_result.stdout,
                    exec_result.stderr,
                    exec_result.elapsed_ms,
                    int(spec["timeout_sec"]),
                )
                results.append(
                    {
                        "index": spec["index"],
                        "mode": "sync",
                        "status": normalized["normalized_status"],
                        "command": spec["command"],
                        "exit_code": exec_result.exit_code,
                        "stdout": exec_result.stdout,
                        "stderr": exec_result.stderr,
                        "elapsed_ms": exec_result.elapsed_ms,
                        **normalized,
                        "raw_output": {
                            "stdout": exec_result.stdout,
                            "stderr": exec_result.stderr,
                        },
                    }
                )

        results = sorted(results, key=lambda item: int(item.get("index", 0)))
        summary = {
            "total": len(results),
            "success": len([item for item in results if item.get("status") == "success"]),
            "partial": len([item for item in results if item.get("status") == "partial"]),
            "failed": len([item for item in results if item.get("status") == "failed"]),
            "running": len([item for item in results if item.get("status") == "running"]),
            "error": len([item for item in results if item.get("status") == "error"]),
        }
        payload: dict[str, Any] = {
            "device": device_name,
            "count": len(results),
            "results": results,
            "summary": summary,
        }
        _audit("cmd.exec", {"device": device_name, "count": len(results), "summary": summary})
        if compat_mode == "v1_1":
            status = "success"
            if summary["failed"] > 0 and (summary["success"] > 0 or summary["partial"] > 0 or summary["running"] > 0):
                status = "partial"
            elif summary["failed"] > 0 and summary["success"] == 0 and summary["partial"] == 0 and summary["running"] == 0:
                status = "failed"
            elif summary["error"] > 0:
                status = "failed"
            elif summary["running"] > 0:
                status = "partial"
            elif summary["partial"] > 0:
                status = "partial"
            return _response_envelope(
                status=status,
                data=payload,
                compat_mode=compat_mode,
                meta={"tool": "cmd_exec"},
            )
        return payload
    except ValueError as exc:
        if compat_mode == "v1_1":
            return _response_envelope(
                status="failed",
                data=None,
                error=_error_object_from_exception(exc),
                compat_mode=compat_mode,
                meta={"tool": "cmd_exec"},
            )
        raise


@mcp.tool()
def file_upload(device_name: str, local_path: str, remote_path: str) -> dict[str, Any]:
    """上传本地文件到远端路径（受 allowed_roots 限制）。

    何时使用：
    - 需要将构建产物或配置文件下发到设备时。
    """
    cfg = _get_device(device_name)
    _remote_path_must_be_absolute(remote_path, cfg.os_family)
    lp = _guard_local_path(Path(local_path))
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
    _remote_path_must_be_absolute(remote_path, cfg.os_family)
    _guard_remote_path(cfg, remote_path)

    lp = _guard_local_path(Path(local_path))
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
    _remote_path_must_be_absolute(remote_path, cfg.os_family)
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
    _remote_path_must_be_absolute(remote_path, cfg.os_family)
    _guard_remote_path(cfg, remote_path)

    with SshDeviceClient(cfg, use_pool=True) as client:
        info = client.stat(remote_path)

    _audit("file.stat", {"device": device_name, "remote_path": remote_path})
    return {"device": device_name, **info}


def _jobs_cleanup_if_needed() -> None:
    """当任务数超过上限时，清理最旧的已完成任务（在 _JOBS_LOCK 内调用）。"""
    ttl_sec = _async_job_ttl_sec()
    now = datetime.now(timezone.utc)
    expired_keys = [
        key
        for key, value in _JOBS.items()
        if value.get("status") in ("done", "error")
        and value.get("finished_at")
        and now - datetime.fromisoformat(str(value["finished_at"]))
        > timedelta(seconds=ttl_sec)
    ]
    for key in expired_keys:
        _JOBS.pop(key, None)
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
def cmd_exec_result(job_id: str, compat_mode: Literal["legacy", "v1_1"] = "legacy") -> dict[str, Any]:
    """取回异步命令执行结果。

    status 字段说明：
    - "running"：命令仍在执行，建议等待 1-3 秒后重试。
    - "done"：执行完成，返回值包含 exit_code / stdout / stderr / elapsed_ms。
    - "error"：执行异常（SSH 断连等），返回值包含 error 字段。

    何时使用：
    - 调用 cmd_exec 且某条命令 mode=async 后，用返回的 job_id 轮询直到 status != "running"。
    - 拿到 status=="done" 后读取 exit_code 判断命令是否成功（0 = 成功）。
    """
    try:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                _jobs_cleanup_if_needed()
                raise ValueError(f"GW-4002 UNKNOWN_JOB: Unknown job_id '{job_id}'.")
            if job.get("status") in ("done", "error") and job.get("finished_at"):
                ttl_sec = _async_job_ttl_sec()
                finished_at = datetime.fromisoformat(str(job["finished_at"]))
                if datetime.now(timezone.utc) - finished_at > timedelta(seconds=ttl_sec):
                    _JOBS.pop(job_id, None)
                    raise ValueError(
                        f"GW-4003 JOB_EXPIRED: Job '{job_id}' result has expired."
                    )
            _jobs_cleanup_if_needed()
            # 在锁内复制，避免 worker 线程 update() 与此处并发产生撕裂读
            legacy_result = dict(job)
        if compat_mode == "v1_1":
            status = "success"
            if legacy_result.get("status") == "running":
                status = "partial"
            elif legacy_result.get("status") == "error":
                status = "failed"
            elif legacy_result.get("status") == "done":
                status = cast(str, legacy_result.get("normalized_status", "success"))
            return _response_envelope(status=status, data=legacy_result, compat_mode=compat_mode, meta={"tool": "cmd_exec_result"})
        return legacy_result
    except ValueError as exc:
        if compat_mode == "v1_1":
            return _response_envelope(
                status="failed",
                data=None,
                error=_error_object_from_exception(exc),
                compat_mode=compat_mode,
                meta={"tool": "cmd_exec_result"},
            )
        raise


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
