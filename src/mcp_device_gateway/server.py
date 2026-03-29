from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import AppConfig, DeviceConfig, load_config
from .security import is_path_allowed, sanitize_args
from .ssh_client import SshDeviceClient

APP_CONFIG: AppConfig = load_config()
mcp = FastMCP("embedded-device-gateway")


def _audit(tool: str, payload: dict[str, Any]) -> None:
    log_path = Path(APP_CONFIG.audit_log)
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "payload": payload,
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def _get_device(device_name: str) -> DeviceConfig:
    try:
        return APP_CONFIG.devices[device_name]
    except KeyError as exc:
        raise ValueError(f"Unknown device '{device_name}'.") from exc


@mcp.tool()
def device_list() -> list[dict[str, Any]]:
    """列出当前已配置的设备。"""
    data = [
        {
            "name": cfg.name,
            "host": cfg.host,
            "port": cfg.port,
            "username": cfg.username,
            "allowed_roots": list(cfg.allowed_roots),
        }
        for cfg in APP_CONFIG.devices.values()
    ]
    _audit("device.list", {"count": len(data)})
    return data


@mcp.tool()
def device_ping(device_name: str) -> dict[str, Any]:
    """测试指定设备的 SSH 连通性。"""
    cfg = _get_device(device_name)
    client = SshDeviceClient(cfg)
    ok = client.ping()

    result = {"device": device_name, "reachable": ok}
    _audit("device.ping", result)
    return result


@mcp.tool()
def cmd_exec(device_name: str, command_key: str, args: list[str] | None = None, timeout_sec: int = 30) -> dict[str, Any]:
    """在远端设备执行命令模板，并校验参数安全性。"""
    cfg = _get_device(device_name)
    template = APP_CONFIG.command_templates.get(command_key)
    if not template:
        raise ValueError(f"Unknown command template '{command_key}'.")

    safe_args = sanitize_args(args or [])
    try:
        command = template.format(*safe_args)
    except IndexError as exc:
        raise ValueError("Command args do not match template placeholders.") from exc

    with SshDeviceClient(cfg) as client:
        result = client.exec(command, timeout_sec=timeout_sec)

    payload = {
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
    """上传文件到远端路径（受 allowed_roots 限制）。"""
    cfg = _get_device(device_name)
    lp = Path(local_path)
    if not lp.exists() or not lp.is_file():
        raise ValueError(f"Local file not found: {local_path}")

    if cfg.allowed_roots and not is_path_allowed(remote_path, cfg.allowed_roots):
        raise ValueError(f"Remote path is not allowed: {remote_path}")

    with SshDeviceClient(cfg) as client:
        client.upload(str(lp), remote_path)

    payload = {"device": device_name, "local_path": str(lp), "remote_path": remote_path}
    _audit("file.upload", payload)
    return {"ok": True, **payload}


@mcp.tool()
def file_download(device_name: str, remote_path: str, local_path: str) -> dict[str, Any]:
    """下载远端文件到本地路径（受 allowed_roots 限制）。"""
    cfg = _get_device(device_name)

    if cfg.allowed_roots and not is_path_allowed(remote_path, cfg.allowed_roots):
        raise ValueError(f"Remote path is not allowed: {remote_path}")

    lp = Path(local_path)
    if lp.parent:
        lp.parent.mkdir(parents=True, exist_ok=True)

    with SshDeviceClient(cfg) as client:
        client.download(remote_path, str(lp))

    payload = {"device": device_name, "remote_path": remote_path, "local_path": str(lp)}
    _audit("file.download", payload)
    return {"ok": True, **payload}


def main() -> None:
    # 默认使用 stdio 传输，以获得更好的 MCP 客户端兼容性。
    mcp.run(transport=os.getenv("MCP_TRANSPORT", "stdio"))


if __name__ == "__main__":
    main()
