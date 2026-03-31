from __future__ import annotations

import socket
import stat as stat_module
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any

import paramiko

from .config import DeviceConfig


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    elapsed_ms: int


class SshConnectionPool:
    """全局 SSH 连接池。

    每个设备（按 username@host:port 索引）保持一个持久的 paramiko.SSHClient，
    多次工具调用复用同一 Transport，避免重复握手开销。
    acquire() 在连接断开时自动重建；invalidate() 用于主动清除失效连接。
    paramiko Transport 支持多路复用 channel，不同线程可安全并发调用
    exec_command() / open_sftp()。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._entries: dict[str, paramiko.SSHClient] = {}

    @staticmethod
    def _key(cfg: DeviceConfig) -> str:
        return f"{cfg.username}@{cfg.host}:{cfg.port}"

    @staticmethod
    def _is_alive(client: paramiko.SSHClient) -> bool:
        transport = client.get_transport()
        return transport is not None and transport.is_active()

    @staticmethod
    def _new_client(cfg: DeviceConfig) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        if cfg.known_hosts:
            client.load_host_keys(cfg.known_hosts)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            # 首次连接自动接受服务器指纹；生产环境应配置 known_hosts。
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=cfg.host,
            port=cfg.port,
            username=cfg.username,
            key_filename=cfg.key_file,
            password=cfg.password,
            timeout=8,
            banner_timeout=8,
            auth_timeout=8,
        )
        return client

    def acquire(self, cfg: DeviceConfig) -> paramiko.SSHClient:
        """取得设备的可用 SSH 连接，不存在或已断开时自动重建。

        采用「锁外建连 + 双重检查」策略：连接活跃度检测与新连接建立均在锁外执行，
        避免 TCP/SSH 握手（最长 8 秒）阻塞其他设备的并发访问。
        """
        key = self._key(cfg)

        # ── 第一次检查：已有活跃连接则直接返回 ──
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None and self._is_alive(existing):
                return existing
            # 摘除失效连接；新连接在锁外建立，不阻塞其他设备
            stale = self._entries.pop(key, None)

        # 关闭失效连接（锁外）
        if stale is not None:
            try:
                stale.close()
            except Exception:
                pass

        # 建立新连接（锁外，耗时操作）
        new_client = self._new_client(cfg)

        # ── 第二次检查：防止并发线程重复建连 ──
        with self._lock:
            winner = self._entries.get(key)
            if winner is not None and self._is_alive(winner):
                # 另一线程抢先完成建连，关闭本次多余连接
                try:
                    new_client.close()
                except Exception:
                    pass
                return winner
            self._entries[key] = new_client
            return new_client

    def invalidate(self, cfg: DeviceConfig) -> None:
        """强制清除并关闭指定设备的连接，下次 acquire 时重建。"""
        key = self._key(cfg)
        with self._lock:
            client = self._entries.pop(key, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def close_all(self) -> None:
        """关闭所有连接（服务退出时调用）。"""
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for c in entries:
            try:
                c.close()
            except Exception:
                pass


# 模块级单例连接池，供所有工具共享
_pool = SshConnectionPool()


class SshDeviceClient:
    """SSH 设备客户端。

    use_pool=True 时从全局连接池取连接，close() 仅释放引用而不断开底层 Transport，
    且在操作失败时自动 invalidate 并重连一次；
    use_pool=False（默认）时行为与之前相同——每次建立独立连接。
    """

    def __init__(self, cfg: DeviceConfig, *, use_pool: bool = False) -> None:
        self._cfg = cfg
        self._use_pool = use_pool
        self._client: paramiko.SSHClient | None = None

    def __enter__(self) -> "SshDeviceClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        if self._use_pool:
            self._client = _pool.acquire(self._cfg)
        else:
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
        if self._use_pool:
            # 连接属于连接池，不关闭底层 Transport，仅清除本地引用。
            self._client = None
        else:
            if self._client:
                self._client.close()
                self._client = None

    def ping(self) -> bool:
        """建立临时连接测试可达性（始终使用独立连接，不污染连接池）。"""
        try:
            self.connect()
            return True
        except (socket.error, paramiko.SSHException, TimeoutError):
            return False
        finally:
            self.close()

    # ------------------------------------------------------------------
    # 内部执行助手（静态，便于重试时复用）
    # ------------------------------------------------------------------

    @staticmethod
    def _do_exec(client: paramiko.SSHClient, command: str, timeout_sec: int) -> ExecResult:
        start = time.monotonic()
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout_sec)
        # 关闭 stdin：使读取 stdin 的命令立即收到 EOF，避免阻塞至 socket 超时
        stdin.close()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ExecResult(exit_code=code, stdout=out, stderr=err, elapsed_ms=elapsed_ms)

    @staticmethod
    def _do_listdir(client: paramiko.SSHClient, remote_path: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        with client.open_sftp() as sftp:
            for attr in sftp.listdir_attr(remote_path):
                mode = attr.st_mode or 0
                if stat_module.S_ISDIR(mode):
                    ftype = "dir"
                elif stat_module.S_ISLNK(mode):
                    ftype = "symlink"
                elif stat_module.S_ISREG(mode):
                    ftype = "file"
                else:
                    ftype = "other"
                mtime_val = float(attr.st_mtime or 0)
                mtime_iso = datetime.fromtimestamp(mtime_val, tz=timezone.utc).isoformat()
                results.append(
                    {
                        "name": attr.filename,
                        "path": remote_path.rstrip("/") + "/" + attr.filename,
                        "type": ftype,
                        "size": attr.st_size or 0,
                        "mtime": mtime_iso,
                        "permissions": oct(mode & 0o7777) if mode else "?",
                    }
                )
        return results

    @staticmethod
    def _do_stat(client: paramiko.SSHClient, remote_path: str) -> dict[str, Any]:
        with client.open_sftp() as sftp:
            attr = sftp.stat(remote_path)
        mode = attr.st_mode or 0
        if stat_module.S_ISDIR(mode):
            ftype = "dir"
        elif stat_module.S_ISLNK(mode):
            ftype = "symlink"
        elif stat_module.S_ISREG(mode):
            ftype = "file"
        else:
            ftype = "other"
        mtime_val = float(attr.st_mtime or 0)
        mtime_iso = datetime.fromtimestamp(mtime_val, tz=timezone.utc).isoformat()
        return {
            "path": remote_path,
            "type": ftype,
            "size": attr.st_size or 0,
            "mtime": mtime_iso,
            "permissions": oct(mode & 0o7777) if mode else "?",
        }

    # ------------------------------------------------------------------
    # 公共操作（连接池模式下失败时 invalidate + 重连一次）
    # ------------------------------------------------------------------

    def exec(self, command: str, timeout_sec: int = 30) -> ExecResult:
        if not self._client:
            raise RuntimeError("SSH client is not connected.")
        try:
            return self._do_exec(self._client, command, timeout_sec)
        except (paramiko.SSHException, socket.error, EOFError):
            if not self._use_pool:
                raise
            _pool.invalidate(self._cfg)
            self._client = _pool.acquire(self._cfg)
            return self._do_exec(self._client, command, timeout_sec)

    def upload(self, local_path: str, remote_path: str) -> None:
        if not self._client:
            raise RuntimeError("SSH client is not connected.")
        try:
            with self._client.open_sftp() as sftp:
                sftp.put(local_path, remote_path)
        except (paramiko.SSHException, socket.error, EOFError):
            if not self._use_pool:
                raise
            _pool.invalidate(self._cfg)
            self._client = _pool.acquire(self._cfg)
            with self._client.open_sftp() as sftp:
                sftp.put(local_path, remote_path)

    def download(self, remote_path: str, local_path: str) -> None:
        if not self._client:
            raise RuntimeError("SSH client is not connected.")
        try:
            with self._client.open_sftp() as sftp:
                sftp.get(remote_path, local_path)
        except (paramiko.SSHException, socket.error, EOFError):
            if not self._use_pool:
                raise
            _pool.invalidate(self._cfg)
            self._client = _pool.acquire(self._cfg)
            with self._client.open_sftp() as sftp:
                sftp.get(remote_path, local_path)

    def listdir(self, remote_path: str) -> list[dict[str, Any]]:
        """列出远端目录内容，返回文件元信息列表。"""
        if not self._client:
            raise RuntimeError("SSH client is not connected.")
        try:
            return self._do_listdir(self._client, remote_path)
        except (paramiko.SSHException, socket.error, EOFError):
            if not self._use_pool:
                raise
            _pool.invalidate(self._cfg)
            self._client = _pool.acquire(self._cfg)
            return self._do_listdir(self._client, remote_path)

    def stat(self, remote_path: str) -> dict[str, Any]:
        """获取远端文件或目录的元信息。"""
        if not self._client:
            raise RuntimeError("SSH client is not connected.")
        try:
            return self._do_stat(self._client, remote_path)
        except (paramiko.SSHException, socket.error, EOFError):
            if not self._use_pool:
                raise
            _pool.invalidate(self._cfg)
            self._client = _pool.acquire(self._cfg)
            return self._do_stat(self._client, remote_path)
