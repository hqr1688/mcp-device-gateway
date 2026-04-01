from __future__ import annotations

import importlib
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import TracebackType

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import mcp_device_gateway.server as server


class CommandResolutionAndBatchTests(unittest.TestCase):
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

    @staticmethod
    def _write_grouped_config(config_file: Path) -> None:
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
                    os_family: linux
                    allowed_roots: [/tmp/]

                command_templates:
                  linux_common:
                    health_check: "echo health"
                    quick_status: "echo status"
                    echo_path: "echo {0}"
                  device_specific:
                    dev1:
                      quick_status: "echo dev1-status"
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

    def _load_server(self, config_file: Path):
        os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
        os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")
        return importlib.reload(server)

    def test_cmd_exec_resolves_short_key_in_device_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_grouped_config(config_file)
            srv = self._load_server(config_file)

            class FakeClient:
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
                    _ = timeout_sec
                    class Result:
                        exit_code = 0
                        stdout = command
                        stderr = ""
                        elapsed_ms = 1

                    return Result()

            original_client = getattr(srv, "SshDeviceClient")
            setattr(srv, "SshDeviceClient", FakeClient)
            try:
                result = srv.cmd_exec("dev1", "quick_status")
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            first = result["results"][0]
            self.assertEqual("device_specific.dev1.quick_status", first["resolved_key"])
            self.assertEqual("short_key_scoped", first["resolution"]["mode"])
            self.assertEqual("echo dev1-status", first["stdout"])

    def test_cmd_exec_strict_mode_rejects_short_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_grouped_config(config_file)
            srv = self._load_server(config_file)

            with self.assertRaises(ValueError) as ctx:
                srv.cmd_exec("dev1", "quick_status", strict=True)

            self.assertIn("UNKNOWN_COMMAND_TEMPLATE", str(ctx.exception))
            self.assertIn("Did you mean", str(ctx.exception))

    def test_unknown_template_has_suggestion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_grouped_config(config_file)
            srv = self._load_server(config_file)

            with self.assertRaises(ValueError) as ctx:
                srv.cmd_exec("dev1", "health_chek")

            self.assertIn("Did you mean", str(ctx.exception))
            self.assertIn("linux_common.health_check", str(ctx.exception))

    def test_invalid_command_args_contains_expected_received_examples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_grouped_config(config_file)
            srv = self._load_server(config_file)

            with self.assertRaises(ValueError) as ctx:
                srv.cmd_exec("dev1", "echo_path")

            text = str(ctx.exception)
            self.assertIn("INVALID_COMMAND_ARGS", text)
            self.assertIn("expected_args", text)
            self.assertIn("received_args", text)
            self.assertIn("examples", text)

    def test_cmd_exec_multi_fail_fast_false_keeps_partial_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_grouped_config(config_file)
            srv = self._load_server(config_file)

            class FakeClient:
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
                    _ = timeout_sec
                    class Result:
                        exit_code = 0
                        stdout = command
                        stderr = ""
                        elapsed_ms = 1

                    return Result()

            original_client = getattr(srv, "SshDeviceClient")
            setattr(srv, "SshDeviceClient", FakeClient)
            try:
                result = srv.cmd_exec(
                    "dev1",
                    commands=[
                        {"command_key": "not_exists"},
                        {"command_key": "health_check"},
                    ],
                    fail_fast=False,
                )
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            results = result["results"]
            self.assertEqual(2, len(results))
            self.assertEqual("failed", results[0]["status"])
            self.assertEqual("success", results[1]["status"])
            self.assertEqual("linux_common.health_check", results[1]["resolved_key"])

    def test_cmd_exec_multi_fail_fast_true_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_grouped_config(config_file)
            srv = self._load_server(config_file)

            with self.assertRaises(ValueError):
                srv.cmd_exec(
                    "dev1",
                    commands=[
                        {"command_key": "not_exists"},
                        {"command_key": "health_check"},
                    ],
                    fail_fast=True,
                )


if __name__ == "__main__":
    unittest.main()
