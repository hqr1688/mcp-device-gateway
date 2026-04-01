from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import TracebackType
from typing import Any

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import mcp_device_gateway.server as server


class ProtocolV11FeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = {
            "MCP_DEVICE_CONFIG": os.environ.get("MCP_DEVICE_CONFIG"),
            "MCP_AUDIT_LOG": os.environ.get("MCP_AUDIT_LOG"),
            "MCP_ASYNC_JOB_TTL_SEC": os.environ.get("MCP_ASYNC_JOB_TTL_SEC"),
            "MCP_MAX_CUSTOM_COMMAND_LEN": os.environ.get("MCP_MAX_CUSTOM_COMMAND_LEN"),
        }

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def _write_config(config_file: Path) -> None:
        config_file.write_text(
            textwrap.dedent(
                """
                devices:
                  dev1:
                    host: 192.168.1.10
                    username: root
                    os_family: linux
                    allowed_roots: [/tmp/]

                alias_registry:
                  quick_status: linux_common.health_check
                  top: linux_common.top_snapshot

                command_templates:
                  linux_common:
                    health_check:
                      template: "echo ok"
                      risk: low
                    top_snapshot:
                      template: "top -bn1"
                      risk: low
                    show_path:
                      template: "ls -al {0}"
                      args: [path]
                      arg_schema:
                        - name: path
                          type: path
                          allow_utf8: true
                          allow_space: true
                      examples:
                        - ["/media/root/新加卷 1"]
                    show_path_quoted:
                      template: 'ls -al "{0}"'
                      args: [path]
                      arg_schema:
                        - name: path
                          type: path
                          allow_utf8: true
                          allow_space: true
                      examples:
                        - ["/media/root/已有 引号"]
                    tegrastats_5s:
                      template: "timeout 5 tegrastats"
                      success_on_exit_codes: [0, 124]
                    needs_root:
                      template: "cat /root/secure.log"
                      requires_privilege: root
                      fallback_templates:
                        - linux_common.health_check
                    free_mem:
                      template: "free -m"
                      parser: free
                    free_mem_broken:
                      template: "free -m --broken"
                      parser: free
                    restart_service:
                      template: "systemctl restart demo"
                      exec_mode: async
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

    def _load_server(self, config_file: Path):
        os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
        os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")
        return importlib.reload(server)

    def _patch_client(self, srv: Any) -> Any:
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
                    stdout = "ok\n"
                    stderr = ""
                    elapsed_ms = 120

                result = Result()
                if command.startswith("ls -al"):
                    result.stdout = command
                elif command.startswith("timeout 5 tegrastats"):
                    result.exit_code = 124
                    result.stdout = "tegrastats sample\n"
                    result.elapsed_ms = 5100
                elif command.startswith("cat /root/secure.log"):
                    result.exit_code = 1
                    result.stderr = "Permission denied"
                elif command.startswith("free -m --broken"):
                    result.stdout = "Mem: total used ???\n"
                elif command.startswith("free -m"):
                    result.stdout = (
                        "              total        used        free      shared  buff/cache   available\n"
                        "Mem:           7820        2100        1300         120        4420        5200\n"
                    )
                return result

        original_client = getattr(srv, "SshDeviceClient")
        setattr(srv, "SshDeviceClient", FakeClient)
        return original_client

    @staticmethod
    def _first(result: dict[str, Any]) -> dict[str, Any]:
        return result["results"][0]

    def test_alias_resolution_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            original_client = self._patch_client(srv)
            try:
                result = srv.cmd_exec("dev1", "quick_status")
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            first = self._first(result)
            self.assertEqual("linux_common.health_check", first["resolved_key"])
            self.assertEqual("alias", first["resolution"]["mode"])

    def test_arg_schema_allows_utf8_and_space_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            original_client = self._patch_client(srv)
            try:
                result = srv.cmd_exec("dev1", "show_path", args=["/media/root/新加卷 1"])
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            self.assertEqual('ls -al "/media/root/新加卷 1"', self._first(result)["stdout"])

    def test_path_arg_does_not_double_quote_when_template_already_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            original_client = self._patch_client(srv)
            try:
                result = srv.cmd_exec("dev1", "show_path_quoted", args=["/media/root/已有 引号"])
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            self.assertEqual('ls -al "/media/root/已有 引号"', self._first(result)["stdout"])

    def test_timeout_exit_code_is_normalized_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            original_client = self._patch_client(srv)
            try:
                result = srv.cmd_exec("dev1", "tegrastats_5s")
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            first = self._first(result)
            self.assertIn(first["status"], ["partial", "success"])
            self.assertNotEqual("failed", first["status"])
            self.assertGreater(first["collected_duration_ms"], 0)

    def test_permission_denied_returns_suggestion_and_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            original_client = self._patch_client(srv)
            try:
                result = srv.cmd_exec("dev1", "needs_root", mode="sync")
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            first = self._first(result)
            self.assertEqual("root", first["requires_privilege"])
            self.assertIn("linux_common.health_check", first["fallback_templates"])
            self.assertIn("需要权限级别", first["permission_suggestion"])

    def test_template_list_supports_filter_paging_and_only_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)

            result = srv.command_template_list(
                category="linux_common",
                keyword="free",
                page=1,
                page_size=1,
                only_fields=["key", "risk"],
            )

            self.assertEqual(1, result["count"])
            self.assertEqual(2, result["total"])
            self.assertIn("key", result["templates"][0])
            self.assertIn("risk", result["templates"][0])
            self.assertNotIn("template", result["templates"][0])

    def test_parser_returns_structured_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            original_client = self._patch_client(srv)
            try:
                result = srv.cmd_exec("dev1", "free_mem", mode="sync")
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            structured = self._first(result)["structured_output"]
            self.assertIsInstance(structured, dict)
            self.assertIn("memory", structured)
            self.assertEqual(7820, structured["memory"]["total"])

    def test_parser_failure_does_not_break_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            original_client = self._patch_client(srv)
            try:
                result = srv.cmd_exec("dev1", "free_mem_broken", mode="sync")
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            first = self._first(result)
            self.assertEqual("success", first["status"])
            self.assertIsNone(first["structured_output"])

    def test_task_recommend_auto_corrects_short_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)

            result = srv.task_recommend("top_snapshot")
            self.assertEqual("linux_common.top_snapshot", result["draft"]["command_key"])

    def test_cmd_exec_v11_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            original_client = self._patch_client(srv)
            try:
                result = srv.cmd_exec("dev1", "health_check", compat_mode="v1_1")
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            self.assertEqual("1.1", result["api_version"])
            self.assertIn("data", result)
            self.assertIn("status", result)

    def test_custom_command_v11_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            original_client = self._patch_client(srv)
            try:
                result = srv.cmd_exec("dev1", command="echo manual", compat_mode="v1_1")
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            self.assertEqual("1.1", result["api_version"])
            self.assertEqual("success", result["status"])
            self.assertEqual("echo manual", result["data"]["results"][0]["command"])

    def test_cmd_exec_multi_v11_envelope_with_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            original_client = self._patch_client(srv)
            try:
                result = srv.cmd_exec(
                    "dev1",
                    commands=[{"command_key": "health_check"}, {"command_key": "not_exists"}],
                    fail_fast=False,
                    compat_mode="v1_1",
                )
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            self.assertEqual("1.1", result["api_version"])
            self.assertIn("summary", result["data"])
            self.assertEqual(2, result["data"]["summary"]["total"])

    def test_error_code_prefix_for_invalid_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)

            with self.assertRaises(ValueError) as ctx:
                srv.cmd_exec("dev1", "show_path")

            self.assertIn("GW-1002", str(ctx.exception))

    def test_v11_unknown_template_returns_error_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)

            result = srv.cmd_exec("dev1", "not_exists", compat_mode="v1_1")
            self.assertEqual("failed", result["status"])
            self.assertIsNotNone(result["error"])
            self.assertEqual("GW-2001", result["error"]["code"])

    def test_v11_unknown_job_returns_error_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)

            result = srv.cmd_exec_result("missing-job", compat_mode="v1_1")
            self.assertEqual("failed", result["status"])
            self.assertEqual("GW-4002", result["error"]["code"])

    def test_v11_invalid_changed_since_returns_error_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)

            result = srv.command_template_list(changed_since="not-a-date", compat_mode="v1_1")
            self.assertEqual("failed", result["status"])
            self.assertEqual("GW-1003", result["error"]["code"])

    def test_v11_invalid_page_returns_error_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)

            result = srv.command_template_list(page=0, compat_mode="v1_1")
            self.assertEqual("failed", result["status"])
            self.assertEqual("GW-1004", result["error"]["code"])

    def test_v11_invalid_page_size_returns_error_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)

            result = srv.command_template_list(page_size=501, compat_mode="v1_1")
            self.assertEqual("failed", result["status"])
            self.assertEqual("GW-1005", result["error"]["code"])

    def test_v11_async_mode_submit_returns_partial_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)

            result = srv.cmd_exec("dev1", "restart_service", compat_mode="v1_1")
            self.assertEqual("partial", result["status"])
            self.assertEqual("running", result["data"]["results"][0]["status"])

    def test_v11_invalid_timeout_returns_error_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)

            result = srv.cmd_exec("dev1", "health_check", timeout_sec=0, compat_mode="v1_1")
            self.assertEqual("failed", result["status"])
            self.assertEqual("GW-1006", result["error"]["code"])

    def test_v11_invalid_custom_command_returns_error_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)

            result = srv.cmd_exec("dev1", command="echo ok\nreboot", compat_mode="v1_1")
            self.assertEqual("failed", result["status"])
            self.assertEqual("GW-1007", result["error"]["code"])

    def test_custom_command_blocks_dangerous_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)

            with self.assertRaises(ValueError) as ctx:
                srv.cmd_exec("dev1", command="reboot")

            self.assertIn("DANGEROUS_COMMAND_BLOCKED", str(ctx.exception))

    def test_v11_custom_command_too_long_returns_error_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            os.environ["MCP_MAX_CUSTOM_COMMAND_LEN"] = "32"

            long_command = "echo " + ("x" * 256)
            result = srv.cmd_exec("dev1", command=long_command, compat_mode="v1_1")
            self.assertEqual("failed", result["status"])
            self.assertEqual("GW-1007", result["error"]["code"])

    def test_cmd_exec_multi_allows_mixed_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            original_client = self._patch_client(srv)
            try:
                result = srv.cmd_exec(
                    "dev1",
                    commands=[
                        {"command_key": "health_check", "mode": "sync"},
                        {"command_key": "restart_service", "mode": "async"},
                    ],
                    fail_fast=False,
                )
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            self.assertEqual("success", result["results"][0]["status"])
            self.assertEqual("running", result["results"][1]["status"])

    def test_async_command_can_be_polled_until_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            original_client = self._patch_client(srv)
            try:
                submit = srv.cmd_exec("dev1", "restart_service")
                job_id = submit["results"][0]["job_id"]
                final_result: dict[str, Any] | None = None
                for _ in range(20):
                    current = srv.cmd_exec_result(job_id)
                    if current["status"] != "running":
                        final_result = current
                        break
                    time.sleep(0.01)
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            self.assertIsNotNone(final_result)
            self.assertEqual("done", final_result["status"])
            self.assertEqual("restart_service", final_result["command_key"])
            self.assertEqual("linux_common.restart_service", final_result["resolved_key"])

    def test_custom_async_command_can_be_polled_until_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            original_client = self._patch_client(srv)
            try:
                submit = srv.cmd_exec("dev1", command="echo custom async", mode="async")
                job_id = submit["results"][0]["job_id"]
                final_result: dict[str, Any] | None = None
                for _ in range(20):
                    current = srv.cmd_exec_result(job_id)
                    if current["status"] != "running":
                        final_result = current
                        break
                    time.sleep(0.01)
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            self.assertIsNotNone(final_result)
            self.assertEqual("done", final_result["status"])
            self.assertEqual("echo custom async", final_result["command"])

    def test_v11_expired_job_returns_error_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            os.environ["MCP_ASYNC_JOB_TTL_SEC"] = "1"
            original_client = self._patch_client(srv)
            try:
                submit = srv.cmd_exec("dev1", "restart_service")
                job_id = submit["results"][0]["job_id"]
                for _ in range(20):
                    current = srv.cmd_exec_result(job_id)
                    if current["status"] == "done":
                        break
                    time.sleep(0.01)
                with srv._JOBS_LOCK:
                    srv._JOBS[job_id]["finished_at"] = (
                        datetime.now(timezone.utc) - timedelta(seconds=2)
                    ).isoformat()
                result = srv.cmd_exec_result(job_id, compat_mode="v1_1")
            finally:
                setattr(srv, "SshDeviceClient", original_client)
                os.environ.pop("MCP_ASYNC_JOB_TTL_SEC", None)

            self.assertEqual("failed", result["status"])
            self.assertEqual("GW-4003", result["error"]["code"])

    def test_v11_async_done_failed_uses_normalized_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            with srv._JOBS_LOCK:
                srv._JOBS["job-failed"] = {
                    "status": "done",
                    "normalized_status": "failed",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "command_key": "restart_service",
                }

            result = srv.cmd_exec_result("job-failed", compat_mode="v1_1")
            self.assertEqual("failed", result["status"])


if __name__ == "__main__":
    unittest.main()
