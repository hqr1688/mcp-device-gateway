from __future__ import annotations

import importlib
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import mcp_device_gateway.server as server
from mcp_device_gateway.config import load_config


class DeviceProfileMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = {
            "MCP_DEVICE_CONFIG": os.environ.get("MCP_DEVICE_CONFIG"),
            "MCP_AUDIT_LOG": os.environ.get("MCP_AUDIT_LOG"),
        }

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write_config(self, config_file: Path) -> None:
        config_file.write_text(
            textwrap.dedent(
                """
                devices:
                  dev1:
                    host: 192.168.1.10
                    username: root
                    os_family: "linux"
                    os_name: "Ubuntu"
                    os_version: "22.04"
                    description: "开发板"
                    when_to_use: "日常联调"
                    capabilities: ["shell-command", "file-transfer"]
                    tags: ["dev", "arm64"]
                    preferred_templates: ["health_check"]
                    allowed_roots:
                      - /tmp/
                command_templates:
                  health_check:
                    template: "uname -a; uptime"
                    description: "健康检查"
                    risk: "low"
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

    def test_device_metadata_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)

            os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
            os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")

            cfg = load_config()
            dev = cfg.devices["dev1"]
            self.assertEqual("开发板", dev.description)
            self.assertEqual("日常联调", dev.when_to_use)
            self.assertEqual("linux", dev.os_family)
            self.assertEqual("Ubuntu", dev.os_name)
            self.assertEqual("22.04", dev.os_version)
            self.assertEqual(("shell-command", "file-transfer"), dev.capabilities)
            self.assertEqual(("dev", "arm64"), dev.tags)
            self.assertEqual(("health_check",), dev.preferred_templates)

    def test_device_profile_get_exposes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)

            os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
            os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")

            srv = importlib.reload(server)
            data = srv.device_profile_get("dev1")

            self.assertEqual("开发板", data["description"])
            self.assertEqual("日常联调", data["when_to_use"])
            self.assertEqual("linux", data["os_family"])
            self.assertEqual("Ubuntu", data["os_name"])
            self.assertEqual("22.04", data["os_version"])
            self.assertEqual(["shell-command", "file-transfer"], data["capabilities"])
            self.assertEqual(["dev", "arm64"], data["tags"])
            self.assertEqual(["health_check"], data["preferred_templates"])


if __name__ == "__main__":
    unittest.main()
