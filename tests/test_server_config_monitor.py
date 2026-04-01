from __future__ import annotations

import importlib
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import TracebackType

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

    @staticmethod
    def _write_valid_config(config_file: Path) -> None:
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

    def test_config_reload_closes_connection_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_valid_config(config_file)
            srv = self._load_server_with_config(config_file)
            close_calls: list[str] = []
            original_close_all = srv._pool.close_all

            def fake_close_all() -> None:
                close_calls.append("closed")

            srv._pool.close_all = fake_close_all
            try:
                srv.device_list()
                self.assertEqual([], close_calls)
                time.sleep(0.02)
                config_file.write_text(
                    textwrap.dedent(
                        """
                        devices:
                            dev1:
                                host: 192.168.1.99
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

                srv.device_list()
            finally:
                srv._pool.close_all = original_close_all

            self.assertEqual(["closed"], close_calls)

    def test_invalid_root_yaml_is_blocked_but_service_stays_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            config_file.write_text("- not-a-mapping\n", encoding="utf-8")
            srv = self._load_server_with_config(config_file)

            with self.assertRaises(RuntimeError) as ctx:
                srv.device_list()

            self.assertIn("CONFIG_UNAVAILABLE", str(ctx.exception))

    def test_cmd_exec_multi_keeps_operational_error_as_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_valid_config(config_file)
            srv = self._load_server_with_config(config_file)

            class FailingClient:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                def __enter__(self):
                    return self

                def __exit__(
                    self,
                    exc_type: type[BaseException] | None,
                    exc: BaseException | None,
                    tb: TracebackType | None,
                ) -> None:
                    return None

                def exec(self, command: str, timeout_sec: int = 30):
                    raise OSError("network down")

            original_client = getattr(srv, "SshDeviceClient")
            setattr(srv, "SshDeviceClient", FailingClient)
            try:
                result = srv.cmd_exec("dev1", commands=[{"command_key": "ping"}])
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            results = result["results"]
            self.assertEqual("ping", results[0]["command_key"])
            self.assertEqual("error", results[0]["status"])
            self.assertEqual("network down", results[0]["error"])

    def test_cmd_exec_multi_does_not_swallow_programming_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_valid_config(config_file)
            srv = self._load_server_with_config(config_file)

            class BrokenClient:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                def __enter__(self):
                    return self

                def __exit__(
                    self,
                    exc_type: type[BaseException] | None,
                    exc: BaseException | None,
                    tb: TracebackType | None,
                ) -> None:
                    return None

                def exec(self, command: str, timeout_sec: int = 30):
                    raise AssertionError("unexpected bug")

            original_client = getattr(srv, "SshDeviceClient")
            setattr(srv, "SshDeviceClient", BrokenClient)
            try:
                with self.assertRaises(AssertionError):
                    srv.cmd_exec("dev1", commands=[{"command_key": "ping"}])
            finally:
                setattr(srv, "SshDeviceClient", original_client)

    def test_reconfigure_stream_utf8_only_swallows_known_errors(self) -> None:
        class ValueErrorStream:
            def reconfigure(self, **_kwargs: object) -> None:
                raise ValueError("bad encoding")

        class RuntimeErrorStream:
            def reconfigure(self, **_kwargs: object) -> None:
                raise RuntimeError("unexpected")

        reconfigure_stream = getattr(server, "_reconfigure_stream_utf8")
        reconfigure_stream(ValueErrorStream())
        with self.assertRaises(RuntimeError):
            reconfigure_stream(RuntimeErrorStream())

    def test_capability_overview_reports_full_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_valid_config(config_file)
            srv = self._load_server_with_config(config_file)

            result = srv.capability_overview()

            tool_names = [item["name"] for item in result["tools"]]
            resource_names = [item["name"] for item in result["resources"]]
            prompt_names = [item["name"] for item in result["prompts"]]

            self.assertEqual(13, result["tool_count"])
            self.assertEqual(1, result["resource_count"])
            self.assertEqual(1, result["prompt_count"])
            self.assertEqual(result["tool_count"], len(tool_names))
            self.assertEqual(result["resource_count"], len(resource_names))
            self.assertEqual(result["prompt_count"], len(prompt_names))
            self.assertIn("capability_overview", tool_names)
            self.assertIn("cmd_exec", tool_names)
            self.assertNotIn("custom_exec", tool_names)
            self.assertNotIn("custom_exec_async", tool_names)
            self.assertEqual(["config://summary"], resource_names)
            self.assertEqual(["device_ops_prompt"], prompt_names)

    def test_audit_log_parent_directory_is_created_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_valid_config(config_file)
            os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
            nested_log = Path(tmp_dir) / "nested" / "audit" / "events.log"
            os.environ["MCP_AUDIT_LOG"] = str(nested_log)

            srv = importlib.reload(server)
            srv.capability_overview()

            self.assertTrue(nested_log.exists())
            self.assertIn("capability.overview", nested_log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
