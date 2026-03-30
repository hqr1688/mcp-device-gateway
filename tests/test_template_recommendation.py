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


class TemplateRecommendationTests(unittest.TestCase):
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
                    allowed_roots:
                      - /tmp/
                command_templates:
                  list_dir:
                    template: "ls -al {0}"
                    description: "列出目录"
                    when_to_use: "检查目录内容"
                    args: ["path"]
                    examples:
                      - ["/opt/idm"]
                    risk: "low"
                  health_check:
                    template: "uname -a; uptime"
                    description: "设备健康检查"
                    when_to_use: "需要快速确认系统状态"
                    risk: "low"
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

    def test_examples_loaded_in_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)

            os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
            os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")

            cfg = load_config()
            item = cfg.command_templates["list_dir"]
            self.assertEqual(("path",), item.args)
            self.assertEqual((("/opt/idm",),), item.examples)

    def test_task_recommend_returns_template_and_args_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)

            os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
            os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")

            srv = importlib.reload(server)
            result = srv.task_recommend("请查看 /opt/idm 目录内容")

            self.assertEqual("cmd_exec", result["recommended_tool"])
            self.assertEqual("list_dir", result["draft"]["command_key"])
            self.assertEqual(["/opt/idm"], result["draft"]["args"])


if __name__ == "__main__":
    unittest.main()
