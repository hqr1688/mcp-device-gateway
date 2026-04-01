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


class FileToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = {
            "MCP_DEVICE_CONFIG": os.environ.get("MCP_DEVICE_CONFIG"),
            "MCP_AUDIT_LOG": os.environ.get("MCP_AUDIT_LOG"),
            "MCP_LOCAL_ALLOWED_ROOTS": os.environ.get("MCP_LOCAL_ALLOWED_ROOTS"),
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
                    allowed_roots:
                      - /opt/app/

                command_templates:
                  ping: "echo ok"
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

    def _load_server(self, config_file: Path):
        os.environ["MCP_DEVICE_CONFIG"] = str(config_file)
        os.environ["MCP_AUDIT_LOG"] = str(config_file.parent / "audit.log")
        return importlib.reload(server)

    def _patch_client(self, srv):
        class FakeClient:
            uploads: list[tuple[str, str]] = []
            downloads: list[tuple[str, str]] = []

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

            def upload(self, local_path: str, remote_path: str) -> None:
                self.uploads.append((local_path, remote_path))

            def download(self, remote_path: str, local_path: str) -> None:
                self.downloads.append((remote_path, local_path))
                Path(local_path).write_text("downloaded", encoding="utf-8")

            def listdir(self, remote_path: str):
                return [{"name": "app.log", "path": remote_path.rstrip("/") + "/app.log"}]

            def stat(self, remote_path: str):
                return {"path": remote_path, "type": "file", "size": 3}

        original_client = getattr(srv, "SshDeviceClient")
        setattr(srv, "SshDeviceClient", FakeClient)
        return original_client, FakeClient

    def test_file_upload_respects_local_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            allowed_dir = Path(tmp_dir) / "allowed"
            allowed_dir.mkdir()
            source = allowed_dir / "artifact.txt"
            source.write_text("hello", encoding="utf-8")
            os.environ["MCP_LOCAL_ALLOWED_ROOTS"] = str(allowed_dir)
            original_client, fake_client = self._patch_client(srv)
            try:
                result = srv.file_upload("dev1", str(source), "/opt/app/artifact.txt")
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            self.assertTrue(result["ok"])
            self.assertEqual(
                [(str(source.resolve()), "/opt/app/artifact.txt")],
                fake_client.uploads,
            )

    def test_file_upload_blocks_local_path_outside_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            allowed_dir = Path(tmp_dir) / "allowed"
            allowed_dir.mkdir()
            outside = Path(tmp_dir) / "outside.txt"
            outside.write_text("hello", encoding="utf-8")
            os.environ["MCP_LOCAL_ALLOWED_ROOTS"] = str(allowed_dir)

            with self.assertRaises(ValueError) as ctx:
                srv.file_upload("dev1", str(outside), "/opt/app/outside.txt")

            self.assertIn("LOCAL_PATH_NOT_ALLOWED", str(ctx.exception))

    def test_file_download_creates_parent_and_respects_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)
            allowed_dir = Path(tmp_dir) / "downloads"
            allowed_dir.mkdir()
            target = allowed_dir / "logs" / "app.log"
            os.environ["MCP_LOCAL_ALLOWED_ROOTS"] = str(allowed_dir)
            original_client, fake_client = self._patch_client(srv)
            try:
                result = srv.file_download("dev1", "/opt/app/app.log", str(target))
            finally:
                setattr(srv, "SshDeviceClient", original_client)

            self.assertTrue(result["ok"])
            self.assertTrue(target.exists())
            self.assertEqual(
                [("/opt/app/app.log", str(target.resolve()))],
                fake_client.downloads,
            )

    def test_dir_list_rejects_relative_remote_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)

            with self.assertRaises(ValueError) as ctx:
                srv.dir_list("dev1", "opt/app")

            self.assertIn("REMOTE_PATH_MUST_BE_ABSOLUTE", str(ctx.exception))

    def test_file_stat_rejects_relative_remote_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "devices.test.yaml"
            self._write_config(config_file)
            srv = self._load_server(config_file)

            with self.assertRaises(ValueError) as ctx:
                srv.file_stat("dev1", "./app.log")

            self.assertIn("REMOTE_PATH_MUST_BE_ABSOLUTE", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
