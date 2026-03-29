from __future__ import annotations

import socket
import time
from dataclasses import dataclass

import paramiko

from .config import DeviceConfig


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    elapsed_ms: int


class SshDeviceClient:
    def __init__(self, cfg: DeviceConfig) -> None:
        self._cfg = cfg
        self._client: paramiko.SSHClient | None = None

    def __enter__(self) -> "SshDeviceClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        client = paramiko.SSHClient()
        if self._cfg.known_hosts:
            client.load_host_keys(self._cfg.known_hosts)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        client.connect(
            hostname=self._cfg.host,
            port=self._cfg.port,
            username=self._cfg.username,
            key_filename=self._cfg.key_file,
            password=self._cfg.password,
            timeout=8,
            banner_timeout=8,
            auth_timeout=8,
        )
        self._client = client

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def ping(self) -> bool:
        try:
            self.connect()
            return True
        except (socket.error, paramiko.SSHException, TimeoutError):
            return False
        finally:
            self.close()

    def exec(self, command: str, timeout_sec: int = 30) -> ExecResult:
        if not self._client:
            raise RuntimeError("SSH client is not connected.")

        start = time.monotonic()
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout_sec)
        _ = stdin
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ExecResult(exit_code=code, stdout=out, stderr=err, elapsed_ms=elapsed_ms)

    def upload(self, local_path: str, remote_path: str) -> None:
        if not self._client:
            raise RuntimeError("SSH client is not connected.")
        with self._client.open_sftp() as sftp:
            sftp.put(local_path, remote_path)

    def download(self, remote_path: str, local_path: str) -> None:
        if not self._client:
            raise RuntimeError("SSH client is not connected.")
        with self._client.open_sftp() as sftp:
            sftp.get(remote_path, local_path)
