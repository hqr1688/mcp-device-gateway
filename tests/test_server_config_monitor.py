from __future__ import annotations

import importlib
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import mcp_device_gateway.server as server


class ServerConfigMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = {
            "MCP_DEVICE_CONFIG": os.environ.get("MCP_DEVICE_CONFIG"),
            "MCP_AUDIT_LOG": os.environ.get("MCP_AUDIT_LOG"),
            "MCP_TRANSPORT": os.environ.get("MCP_TRANSPORT"),
            "MCP_CONFIG_POLL_INTERVAL_SEC": os.environ.get("MCP_CONFIG_POLL_INTERVAL_SEC"),
        }

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _load_server_with_config(self, config_file: Path):
        os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
        os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")
        return importlib.reload(server)

    def test_missing_config_generates_template_and_blocks_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            srv = self._load_server_with_config(config_file)

            with self.assertRaises(RuntimeError):
                srv.device_list()

            self.assertTrue(config_file.exists())
            text = config_file.read_text(encoding="utf-8")
            self.assertIn("devices:", text)
            self.assertIn("command_templates:", text)

    def test_config_recovers_and_loads_after_file_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            srv = self._load_server_with_config(config_file)

            # 先触发缺失场景，自动生成模板并进入不可用状态。
            with self.assertRaises(RuntimeError):
                srv.device_list()

            config_file.write_text(
                                textwrap.dedent(
                                        """
                                        devices:
                                            dev1:
                                                host: 192.168.1.10
                                                username: root
                                                allowed_roots:
                                                    - /tmp/
                                        command_templates:
                                            ping: echo ok
                                        """
                                ).strip()
                + "\n",
                encoding="utf-8",
            )

            first_loaded = srv.device_list()
            self.assertEqual(1, len(first_loaded))
            self.assertEqual("192.168.1.10", first_loaded[0]["host"])

            time.sleep(0.02)
            config_file.write_text(
                                textwrap.dedent(
                                        """
                                        devices:
                                            dev1:
                                                host: 192.168.1.20
                                                username: root
                                                allowed_roots:
                                                    - /tmp/
                                        command_templates:
                                            ping: echo ok
                                        """
                                ).strip()
                + "\n",
                encoding="utf-8",
            )

            updated = srv.device_list()
            self.assertEqual("192.168.1.20", updated[0]["host"])

    def test_invalid_root_yaml_is_blocked_but_service_stays_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            config_file.write_text("- not-a-mapping\n", encoding="utf-8")
            srv = self._load_server_with_config(config_file)

            with self.assertRaises(RuntimeError) as ctx:
                srv.device_list()

            self.assertIn("CONFIG_UNAVAILABLE", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
