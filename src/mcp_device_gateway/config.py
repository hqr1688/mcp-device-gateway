from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
import re

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
    description: str = ""
    when_to_use: str = ""
    capabilities: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    preferred_templates: tuple[str, ...] = ()
    os_family: str = "linux"
    os_name: str = ""
    os_version: str = ""


@dataclass(frozen=True)
class AppConfig:
    devices: dict[str, DeviceConfig]
    command_templates: dict[str, "CommandTemplate"]
    audit_log: str


@dataclass(frozen=True)
class CommandTemplate:
    key: str
    short_key: str
    template: str
    description: str = ""
    when_to_use: str = ""
    args: tuple[str, ...] = ()
    examples: tuple[tuple[str, ...], ...] = ()
    risk: str = "medium"
    category: str = "legacy"
    device_name: str | None = None


def _read_yaml(file_path: Path) -> dict[str, Any]:
    if not file_path.exists():
        raise FileNotFoundError(f"Config file not found: {file_path}")
    with file_path.open("r", encoding="utf-8") as fh:
        loaded: Any = yaml.safe_load(fh)
    content_obj: Any = {} if loaded is None else loaded
    if not isinstance(content_obj, dict):
        raise ValueError("Config root must be a mapping.")
    content: dict[str, Any] = cast(dict[str, Any], content_obj)
    return content


def _to_device(name: str, raw: dict[str, Any]) -> DeviceConfig:
    if not raw.get("host") or not raw.get("username"):
        raise ValueError(f"Device '{name}' must define host and username.")

    allowed_roots: Any = raw.get("allowed_roots") or []
    if not isinstance(allowed_roots, list):
        raise ValueError(f"Device '{name}' allowed_roots must be a list.")
    allowed_roots_list = cast(list[Any], allowed_roots)

    capabilities_value: Any = raw.get("capabilities") or []
    if not isinstance(capabilities_value, list):
        raise ValueError(f"Device '{name}' capabilities must be a list.")
    capabilities_list = cast(list[Any], capabilities_value)

    tags_value: Any = raw.get("tags") or []
    if not isinstance(tags_value, list):
        raise ValueError(f"Device '{name}' tags must be a list.")
    tags_list = cast(list[Any], tags_value)

    preferred_templates_value: Any = raw.get("preferred_templates") or []
    if not isinstance(preferred_templates_value, list):
        raise ValueError(f"Device '{name}' preferred_templates must be a list.")
    preferred_templates_list = cast(list[Any], preferred_templates_value)

    raw_os_family = str(raw.get("os_family", "")).strip().lower()
    os_family = raw_os_family or "linux"
    if os_family and os_family not in {"linux", "windows"}:
        raise ValueError(f"Device '{name}' os_family must be 'linux' or 'windows'.")

    return DeviceConfig(
        name=name,
        host=str(raw["host"]),
        username=str(raw["username"]),
        port=int(raw.get("port", 22)),
        key_file=str(raw["key_file"]) if raw.get("key_file") else None,
        password=str(raw["password"]) if raw.get("password") else None,
        known_hosts=str(raw["known_hosts"]) if raw.get("known_hosts") else None,
        allowed_roots=tuple(str(p) for p in allowed_roots_list),
        description=str(raw.get("description", "")).strip(),
        when_to_use=str(raw.get("when_to_use", "")).strip(),
        capabilities=tuple(str(item).strip() for item in capabilities_list if str(item).strip()),
        tags=tuple(str(item).strip() for item in tags_list if str(item).strip()),
        preferred_templates=tuple(str(item).strip() for item in preferred_templates_list if str(item).strip()),
        os_family=os_family,
        os_name=str(raw.get("os_name", "")).strip(),
        os_version=str(raw.get("os_version", "")).strip(),
    )


def _placeholder_count(template: str) -> int:
    indexes = [int(m.group(1)) for m in re.finditer(r"\{(\d+)\}", template)]
    if not indexes:
        return 0
    return max(indexes) + 1


def _to_command_template(
    key: str,
    raw: Any,
    *,
    short_key: str | None = None,
    category: str = "legacy",
    device_name: str | None = None,
) -> CommandTemplate:
    if isinstance(raw, str):
        return CommandTemplate(
            key=key,
            short_key=short_key or key,
            template=raw,
            category=category,
            device_name=device_name,
        )

    if not isinstance(raw, dict):
        raise ValueError(f"Template '{key}' must be a string or mapping.")

    raw_map = cast(dict[str, Any], raw)

    template = raw_map.get("template")
    if not template or not isinstance(template, str):
        raise ValueError(f"Template '{key}' must define a string field 'template'.")

    description = str(raw_map.get("description", "")).strip()
    when_to_use = str(raw_map.get("when_to_use", "")).strip()
    risk = str(raw_map.get("risk", "medium")).strip().lower() or "medium"
    if risk not in {"low", "medium", "high"}:
        raise ValueError(f"Template '{key}' risk must be one of low/medium/high.")

    args_value: Any = raw_map.get("args", [])
    if args_value is None:
        args_value = []
    if not isinstance(args_value, list):
        raise ValueError(f"Template '{key}' args must be a list.")

    args_list = cast(list[Any], args_value)
    arg_names = tuple(str(item).strip() for item in args_list if str(item).strip())

    examples_value: Any = raw_map.get("examples", [])
    if examples_value is None:
        examples_value = []
    if not isinstance(examples_value, list):
        raise ValueError(f"Template '{key}' examples must be a list.")

    examples_list = cast(list[Any], examples_value)
    parsed_examples: list[tuple[str, ...]] = []
    for example in examples_list:
        if isinstance(example, str):
            parsed = (example.strip(),) if example.strip() else ()
        elif isinstance(example, list):
            example_items = cast(list[Any], example)
            parsed = tuple(str(item).strip() for item in example_items if str(item).strip())
        elif isinstance(example, tuple):
            example_items = cast(tuple[Any, ...], example)
            parsed = tuple(str(item).strip() for item in example_items if str(item).strip())
        else:
            raise ValueError(f"Template '{key}' each example must be a string or list.")

        if parsed:
            parsed_examples.append(parsed)

    required_count = _placeholder_count(template)
    if arg_names and len(arg_names) < required_count:
        raise ValueError(
            f"Template '{key}' args count is less than required placeholders: {required_count}."
        )
    for ex in parsed_examples:
        if len(ex) < required_count:
            raise ValueError(
                f"Template '{key}' example args count is less than required placeholders: {required_count}."
            )

    return CommandTemplate(
        key=key,
        short_key=short_key or key,
        template=template,
        description=description,
        when_to_use=when_to_use,
        args=arg_names,
        examples=tuple(parsed_examples),
        risk=risk,
        category=category,
        device_name=device_name,
    )


def _parse_template_group(
    templates: dict[str, CommandTemplate],
    raw_group: dict[str, Any],
    *,
    category: str,
    device_name: str | None = None,
) -> None:
    for key, value in raw_group.items():
        short_key = str(key)
        full_key = f"{category}.{short_key}"
        if device_name:
            full_key = f"device_specific.{device_name}.{short_key}"

        if full_key in templates:
            raise ValueError(f"Duplicate template key '{full_key}'.")

        templates[full_key] = _to_command_template(
            full_key,
            value,
            short_key=short_key,
            category=category,
            device_name=device_name,
        )


def load_config() -> AppConfig:
    config_path = Path(os.getenv("MCP_DEVICE_CONFIG", "./devices.example.yaml")).resolve()
    audit_log = os.getenv("MCP_AUDIT_LOG", "./mcp_audit.log")

    raw = _read_yaml(config_path)
    raw_devices: Any = raw.get("devices") or {}
    if not isinstance(raw_devices, dict):
        raise ValueError("devices must be a mapping.")
    raw_devices_map = cast(dict[str, Any], raw_devices)

    devices: dict[str, DeviceConfig] = {}
    for name, item in raw_devices_map.items():
        if not isinstance(item, dict):
            raise ValueError(f"Device '{name}' must be a mapping.")
        devices[str(name)] = _to_device(str(name), cast(dict[str, Any], item))

    if not devices:
        raise ValueError("At least one device must be configured.")

    templates: Any = raw.get("command_templates") or {}
    if not isinstance(templates, dict):
        raise ValueError("command_templates must be a mapping.")
    templates_map = cast(dict[str, Any], templates)

    parsed_templates: dict[str, CommandTemplate] = {}
    grouped_keys = {"windows_common", "linux_common", "device_specific"}
    found_grouped_keys = {k for k in templates_map if k in grouped_keys}
    found_legacy_keys = {k for k in templates_map if k not in grouped_keys}
    is_grouped = bool(found_grouped_keys) and not found_legacy_keys

    if found_grouped_keys and found_legacy_keys:
        raise ValueError(
            "command_templates cannot mix grouped keys with legacy flat keys; "
            "please use either fully grouped format or fully flat format."
        )

    if is_grouped:
        windows_raw: Any = templates_map.get("windows_common", {})
        if not isinstance(windows_raw, dict):
            raise ValueError("command_templates.windows_common must be a mapping.")
        _parse_template_group(parsed_templates, cast(dict[str, Any], windows_raw), category="windows_common")

        linux_raw: Any = templates_map.get("linux_common", {})
        if not isinstance(linux_raw, dict):
            raise ValueError("command_templates.linux_common must be a mapping.")
        _parse_template_group(parsed_templates, cast(dict[str, Any], linux_raw), category="linux_common")

        device_specific_raw: Any = templates_map.get("device_specific", {})
        if not isinstance(device_specific_raw, dict):
            raise ValueError("command_templates.device_specific must be a mapping.")

        for device_name, group in cast(dict[str, Any], device_specific_raw).items():
            if str(device_name) not in devices:
                raise ValueError(f"device_specific templates references unknown device '{device_name}'.")
            if not isinstance(group, dict):
                raise ValueError(f"command_templates.device_specific.{device_name} must be a mapping.")

            _parse_template_group(
                parsed_templates,
                cast(dict[str, Any], group),
                category="device_specific",
                device_name=str(device_name),
            )
    else:
        for key, value in templates_map.items():
            parsed_templates[str(key)] = _to_command_template(str(key), value)

    return AppConfig(
        devices=devices,
        command_templates=parsed_templates,
        audit_log=audit_log,
    )
