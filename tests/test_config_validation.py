from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcp_device_gateway.config import load_config


class ConfigValidationTests(unittest.TestCase):
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

    def _load_config_text(self, text: str) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            config_file.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")
            os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
            os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")
            load_config()

    def test_blank_host_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._load_config_text(
                """
                devices:
                  dev1:
                    host: "   "
                    username: root
                    allowed_roots: [/tmp/]
                command_templates:
                  ping: "echo ok"
                """
            )

        self.assertIn("non-empty host", str(ctx.exception))

    def test_blank_username_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._load_config_text(
                """
                devices:
                  dev1:
                    host: 192.168.1.10
                    username: " "
                    allowed_roots: [/tmp/]
                command_templates:
                  ping: "echo ok"
                """
            )

        self.assertIn("non-empty username", str(ctx.exception))

    def test_port_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._load_config_text(
                """
                devices:
                  dev1:
                    host: 192.168.1.10
                    username: root
                    port: 70000
                    allowed_roots: [/tmp/]
                command_templates:
                  ping: "echo ok"
                """
            )

        self.assertIn("between 1 and 65535", str(ctx.exception))

    def test_alias_target_must_exist(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._load_config_text(
                """
                devices:
                  dev1:
                    host: 192.168.1.10
                    username: root
                    allowed_roots: [/tmp/]
                alias_registry:
                  quick_status: linux_common.health_check
                command_templates:
                  linux_common:
                    ping:
                      template: "echo ok"
                """
            )

        self.assertIn("references unknown command template", str(ctx.exception))

    def test_alias_cannot_conflict_with_short_key(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._load_config_text(
                """
                devices:
                  dev1:
                    host: 192.168.1.10
                    username: root
                    allowed_roots: [/tmp/]
                alias_registry:
                  health_check: linux_common.health_check
                command_templates:
                  linux_common:
                    health_check:
                      template: "echo ok"
                """
            )

        self.assertIn("conflicts with an existing template short key", str(ctx.exception))

    def test_success_on_exit_codes_must_be_valid_exit_codes(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._load_config_text(
                """
                devices:
                  dev1:
                    host: 192.168.1.10
                    username: root
                    allowed_roots: [/tmp/]
                command_templates:
                  linux_common:
                    ping:
                      template: "echo ok"
                      success_on_exit_codes: [0, -1, 999]
                """
            )

        self.assertIn("success_on_exit_codes must be between 0 and 255", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
