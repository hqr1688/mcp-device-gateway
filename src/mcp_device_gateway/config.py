from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class DeviceConfig:
    name: str
    host: str
    username: str
    port: int = 22
    key_file: str | None = None
    password: str | None = None
    known_hosts: str | None = None
    allowed_roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppConfig:
    devices: dict[str, DeviceConfig]
    command_templates: dict[str, str]
    audit_log: str


def _read_yaml(file_path: Path) -> dict[str, Any]:
    if not file_path.exists():
        raise FileNotFoundError(f"Config file not found: {file_path}")
    with file_path.open("r", encoding="utf-8") as fh:
        content = yaml.safe_load(fh) or {}
    if not isinstance(content, dict):
        raise ValueError("Config root must be a mapping.")
    return content


def _to_device(name: str, raw: dict[str, Any]) -> DeviceConfig:
    if not raw.get("host") or not raw.get("username"):
        raise ValueError(f"Device '{name}' must define host and username.")

    allowed_roots = raw.get("allowed_roots") or []
    if not isinstance(allowed_roots, list):
        raise ValueError(f"Device '{name}' allowed_roots must be a list.")

    return DeviceConfig(
        name=name,
        host=str(raw["host"]),
        username=str(raw["username"]),
        port=int(raw.get("port", 22)),
        key_file=str(raw["key_file"]) if raw.get("key_file") else None,
        password=str(raw["password"]) if raw.get("password") else None,
        known_hosts=str(raw["known_hosts"]) if raw.get("known_hosts") else None,
        allowed_roots=tuple(str(p) for p in allowed_roots),
    )


def load_config() -> AppConfig:
    config_path = Path(os.getenv("MCP_DEVICE_CONFIG", "./devices.example.yaml")).resolve()
    audit_log = os.getenv("MCP_AUDIT_LOG", "./mcp_audit.log")

    raw = _read_yaml(config_path)
    raw_devices = raw.get("devices") or {}
    if not isinstance(raw_devices, dict):
        raise ValueError("devices must be a mapping.")

    devices: dict[str, DeviceConfig] = {}
    for name, item in raw_devices.items():
        if not isinstance(item, dict):
            raise ValueError(f"Device '{name}' must be a mapping.")
        devices[name] = _to_device(str(name), item)

    if not devices:
        raise ValueError("At least one device must be configured.")

    templates = raw.get("command_templates") or {}
    if not isinstance(templates, dict):
        raise ValueError("command_templates must be a mapping.")

    return AppConfig(
        devices=devices,
        command_templates={str(k): str(v) for k, v in templates.items()},
        audit_log=audit_log,
    )
