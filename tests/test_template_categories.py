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


class TemplateCategoriesTests(unittest.TestCase):
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
                    os_family: linux
                    allowed_roots: [/tmp/]
                  dev2:
                    host: 192.168.1.11
                    username: root
                    os_family: windows
                    allowed_roots: [/tmp/]

                command_templates:
                  windows_common:
                    list_dir: "powershell -Command Get-ChildItem -Force {0}"
                  linux_common:
                    list_dir: "ls -al {0}"
                  device_specific:
                    dev1:
                      only_dev1: "echo dev1"
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

    def test_grouped_templates_loaded_with_scoped_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)

            os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
            os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")

            cfg = load_config()
            self.assertIn("windows_common.list_dir", cfg.command_templates)
            self.assertIn("linux_common.list_dir", cfg.command_templates)
            self.assertIn("device_specific.dev1.only_dev1", cfg.command_templates)

    def test_device_specific_template_rejected_on_other_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)

            os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
            os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")

            srv = importlib.reload(server)
            with self.assertRaises(ValueError):
                srv.cmd_exec("dev2", "device_specific.dev1.only_dev1", args=[])

    def test_linux_template_rejected_on_windows_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)

            os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
            os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")

            srv = importlib.reload(server)
            with self.assertRaises(ValueError) as ctx:
                srv.cmd_exec("dev2", "linux_common.list_dir", args=["/tmp"])

            self.assertIn("TEMPLATE_NOT_APPLICABLE", str(ctx.exception))
            self.assertIn("GW-2003", str(ctx.exception))

    def test_windows_template_rejected_on_linux_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)

            os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
            os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")

            srv = importlib.reload(server)
            with self.assertRaises(ValueError) as ctx:
                srv.cmd_exec("dev1", "windows_common.list_dir", args=["C:/"])

            self.assertIn("TEMPLATE_NOT_APPLICABLE", str(ctx.exception))
            self.assertIn("GW-2003", str(ctx.exception))

    def test_missing_os_family_defaults_to_linux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            config_file.write_text(
                textwrap.dedent(
                    """
                    devices:
                      dev1:
                        host: 192.168.1.10
                        username: root
                        allowed_roots: [/tmp/]

                    command_templates:
                      linux_common:
                        health_check: "uname -a"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
            os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")

            cfg = load_config()
            self.assertEqual("linux", cfg.devices["dev1"].os_family)

    def test_empty_os_family_defaults_to_linux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            config_file.write_text(
                textwrap.dedent(
                    """
                    devices:
                      dev1:
                        host: 192.168.1.10
                        username: root
                        os_family: ""
                        allowed_roots: [/tmp/]

                    command_templates:
                      linux_common:
                        health_check: "uname -a"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
            os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")

            cfg = load_config()
            self.assertEqual("linux", cfg.devices["dev1"].os_family)

    def test_mixing_grouped_and_legacy_templates_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            config_file.write_text(
                textwrap.dedent(
                    """
                    devices:
                      dev1:
                        host: 192.168.1.10
                        username: root
                        allowed_roots: [/tmp/]

                    command_templates:
                      linux_common:
                        health_check: "uname -a; uptime"
                      flat_key: "echo ok"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
            os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")

            with self.assertRaises(ValueError) as ctx:
                load_config()

            self.assertIn("cannot mix grouped keys", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
