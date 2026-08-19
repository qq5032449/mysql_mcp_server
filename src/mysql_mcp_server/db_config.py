"""databases.json 配置管理：加载（内存缓存）、校验、原子保存、别名解析、环境变量向后兼容、密码脱敏。"""

import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from threading import RLock

# 配置目录可用环境变量覆盖；默认为进程工作目录下的 config/
CONFIG_DIR = Path(os.getenv("MYSQL_MCP_CONFIG_DIR", str(Path.cwd() / "config")))
CONFIG_FILE = CONFIG_DIR / "databases.json"

logger = logging.getLogger("mysql_mcp_server.db_config")

_lock = RLock()
_cache: dict | None = None

_ALIAS_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_VALID_POLICIES = {"client_confirm", "elicitation_only"}


def _empty_config() -> dict:
    return {"default_alias": None, "databases": {}}


def _env_entry() -> dict | None:
    """环境变量存在时构造向后兼容的单库配置（读/写同账号）。"""
    user = os.getenv("MYSQL_USER")
    password = os.getenv("MYSQL_PASSWORD")
    if not user or password is None:
        return None
    return {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "database": os.getenv("MYSQL_DATABASE"),
        "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
        "read_user": {"user": user, "password": password},
        "write_user": {"user": user, "password": password},
        "write_policy": "client_confirm",
        "allow_delete": os.getenv("MYSQL_ALLOW_DELETE", "false").lower() == "true",
    }


def load_config() -> dict:
    """读取配置（带内存缓存）。JSON 无任何数据库时回退环境变量条目。"""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        if CONFIG_FILE.exists():
            try:
                _cache = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    "Failed to load config %s (%s); backing up and starting empty",
                    CONFIG_FILE, e,
                )
                try:
                    shutil.copy2(CONFIG_FILE, CONFIG_DIR / (CONFIG_FILE.name + ".corrupt"))
                except OSError:
                    pass
                _cache = _empty_config()
        else:
            _cache = _empty_config()
        if not _cache.get("databases"):
            env = _env_entry()
            if env:
                _cache = {"default_alias": "default", "databases": {"default": env}}
        return _cache


def save_config(cfg: dict) -> None:
    """原子保存：先写临时文件再 os.replace，失败时清理临时文件。"""
    global _cache
    with _lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(CONFIG_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_FILE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _cache = cfg


def reset_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def validate_entry(alias: str, entry: dict) -> None:
    """校验别名与条目必填项，非法时抛 ValueError（中文消息供管理页面展示）。"""
    if not alias or not _ALIAS_RE.fullmatch(alias):
        raise ValueError("别名只能包含字母、数字、下划线、连字符，长度 1-64")
    if not entry.get("host"):
        raise ValueError("host 不能为空")
    try:
        port = int(entry.get("port", 3306))
    except (TypeError, ValueError):
        raise ValueError("port 必须是整数")
    if not (1 <= port <= 65535):
        raise ValueError("port 必须在 1-65535 之间")
    for role in ("read_user", "write_user"):
        acct = entry.get(role) or {}
        if not acct.get("user"):
            raise ValueError(f"{role}.user 不能为空")
    if entry.get("write_policy", "client_confirm") not in _VALID_POLICIES:
        raise ValueError("write_policy 只能是 client_confirm 或 elicitation_only")


def normalize_entry(entry: dict) -> dict:
    """只保留已知字段并填默认值（未知字段丢弃）。"""
    def _acct(role):
        acct = entry.get(role) or {}
        return {"user": acct.get("user", ""), "password": acct.get("password", "")}

    def _ssh_int(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError("ssh 端口必须是整数") from None

    ssh = entry.get("ssh") or {}
    return {
        "host": entry.get("host", "localhost"),
        "port": int(entry.get("port", 3306)),
        "database": entry.get("database") or None,
        "charset": entry.get("charset") or "utf8mb4",
        "sql_mode": entry.get("sql_mode") or "TRADITIONAL",
        "connect_timeout": int(entry.get("connect_timeout", 10)),
        "read_user": _acct("read_user"),
        "write_user": _acct("write_user"),
        "write_policy": entry.get("write_policy", "client_confirm"),
        "allow_delete": bool(entry.get("allow_delete", False)),
        "ssh": {
            "enable": bool(ssh.get("enable", False)),
            "host": ssh.get("host"),
            "port": _ssh_int(ssh.get("port", 22)),
            "user": ssh.get("user"),
            "key_path": ssh.get("key_path"),
            "remote_host": ssh.get("remote_host", "localhost"),
            "remote_port": _ssh_int(ssh.get("remote_port", 3306)),
            "local_port": _ssh_int(ssh.get("local_port", 3330)),
        },
    }


def resolve(alias: str | None) -> tuple[str, dict] | None:
    """解析别名到 (alias, entry)。alias=None 时用 default_alias；解析失败返回 None。

    返回的 entry 是深拷贝，调用方原地修改不会污染缓存。
    """
    cfg = load_config()
    dbs = cfg.get("databases", {})
    if alias:
        return (alias, json.loads(json.dumps(dbs[alias]))) if alias in dbs else None
    default = cfg.get("default_alias")
    if default and default in dbs:
        return default, json.loads(json.dumps(dbs[default]))
    return None


def masked_config(cfg: dict) -> dict:
    """深拷贝并把非空密码替换为 '****'（原配置不动）。"""
    out = json.loads(json.dumps(cfg))
    for e in out.get("databases", {}).values():
        for role in ("read_user", "write_user"):
            acct = e.get(role)
            if isinstance(acct, dict) and acct.get("password"):
                acct["password"] = "****"
    return out
