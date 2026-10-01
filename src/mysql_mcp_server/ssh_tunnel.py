"""SSH 隧道管理：按 ssh 配置指纹复用常驻隧道进程，替代旧的"每次查询新建 + sleep(2)"。

设计：
- 指纹 = (ssh host, port, user, key_path, remote_host, remote_port)；相同指纹的
  条目共享同一条隧道，配置变更（指纹变化）自然产生新隧道。
- 本地端口自动分配（绑定 0 获取空闲端口），不再使用固定的 local_port=3330，
  避免同机多别名/并发查询的端口冲突。配置里的 local_port 字段保留但不再使用。
- 就绪探测：轮询 TCP 连接本地端口直到成功（ssh -N 建立转发后端口才开始监听），
  进程提前退出则直接报错并带上 stderr。
- 隧道随进程存活（服务是常驻的），不在每次查询后关闭；进程退出时 atexit 统一回收。
"""

import atexit
import logging
import socket
import subprocess
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger("mysql_mcp_server.ssh_tunnel")

_READY_TIMEOUT = 10.0  # 等待隧道端口可连接的上限（秒）
_READY_INTERVAL = 0.2

_tunnels: dict[tuple, "_Tunnel"] = {}
_lock = threading.RLock()


def _fingerprint(ssh: dict) -> tuple:
    return (
        ssh.get("host"),
        int(ssh.get("port", 22)),
        ssh.get("user"),
        ssh.get("key_path"),
        ssh.get("remote_host", "localhost"),
        int(ssh.get("remote_port", 3306)),
    )


def _free_local_port() -> int:
    """向系统申请一个空闲 TCP 端口（存在极小的 TOCTOU 窗口，就绪探测会兜底报错）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class _Tunnel:
    def __init__(self, ssh: dict):
        self.ssh = ssh
        self.proc: subprocess.Popen | None = None
        self.port = 0

    def start(self) -> None:
        last_err = ""
        for _ in range(3):  # 端口被抢占等偶发失败时重试换端口
            self.port = _free_local_port()
            try:
                self._spawn()
                self._wait_ready()
                logger.info(
                    "SSH tunnel ready: %s@%s -> 127.0.0.1:%s -> %s:%s",
                    self.ssh.get("user"), self.ssh.get("host"), self.port,
                    self.ssh.get("remote_host", "localhost"),
                    self.ssh.get("remote_port", 3306),
                )
                return
            except RuntimeError as e:
                last_err = str(e)
                self._stop_proc()
        raise RuntimeError(f"SSH tunnel failed to start: {last_err}")

    def _spawn(self) -> None:
        ssh = self.ssh
        cmd = [
            "ssh", "-N",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=30",
            "-L", f"{self.port}:{ssh.get('remote_host', 'localhost')}:{ssh.get('remote_port', 3306)}",
            f"{ssh.get('user')}@{ssh.get('host')}",
            "-p", str(ssh.get("port", 22)),
        ]
        if ssh.get("key_path"):
            cmd[1:1] = ["-i", ssh["key_path"]]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + _READY_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                stderr = ""
                if self.proc.stderr is not None:
                    stderr = self.proc.stderr.read().decode(errors="replace").strip()
                raise RuntimeError(f"ssh process exited prematurely: {stderr or 'no stderr'}")
            if _port_open(self.port):
                return
            time.sleep(_READY_INTERVAL)
        raise RuntimeError(f"tunnel port 127.0.0.1:{self.port} not ready within {_READY_TIMEOUT}s")

    def _stop_proc(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception as e:
            logger.error("Error terminating SSH tunnel: %s", e)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


def ensure_tunnel(ssh: dict) -> tuple[str, int]:
    """返回该 ssh 配置对应常驻隧道的 (host, port)；不存在或已死则启动新隧道。"""
    key = _fingerprint(ssh)
    with _lock:
        tunnel = _tunnels.get(key)
        if tunnel is not None and tunnel.alive():
            return "127.0.0.1", tunnel.port
        if tunnel is not None:
            tunnel._stop_proc()
        tunnel = _Tunnel(ssh)
        tunnel.start()
        _tunnels[key] = tunnel
        return "127.0.0.1", tunnel.port


@contextmanager
def maybe_ssh_tunnel_for(entry: dict):
    """按别名条目的 ssh 配置返回连接目标；未启用 SSH 时直连，启用时复用常驻隧道。

    兼容旧签名：yield (host, port)，退出时不关闭隧道（常驻）。
    """
    ssh = entry.get("ssh") or {}
    if not ssh.get("enable"):
        yield entry["host"], int(entry.get("port", 3306))
        return
    yield ensure_tunnel(ssh)


def close_all_tunnels() -> None:
    """关闭并清空全部常驻隧道（测试与进程退出用）。"""
    with _lock:
        for tunnel in _tunnels.values():
            tunnel._stop_proc()
        _tunnels.clear()


atexit.register(close_all_tunnels)
