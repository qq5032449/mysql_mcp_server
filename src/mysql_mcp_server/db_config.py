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
_VALID_DB_TYPES = {"mysql", "dameng"}

# 用户表：名称即唯一键（改名级联更新引用）。名称允许中文等非 ASCII，
# 但不能含首尾空白与控制字符。
USER_NAME_MAX = 64
_REF_ROLES = ("read_user_ref", "write_user_ref")


def _empty_config() -> dict:
    return {"default_alias": None, "users": {}, "databases": {}}


def _env_entry() -> dict | None:
    """环境变量存在时构造向后兼容的单库配置（读/写同账号）。"""
    user = os.getenv("MYSQL_USER")
    password = os.getenv("MYSQL_PASSWORD")
    if not user or password is None:
        return None
    return {
        "db_type": "mysql",
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "database": os.getenv("MYSQL_DATABASE"),
        "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
        "read_user": {"user": user, "password": password},
        "write_user": {"user": user, "password": password},
        "write_policy": "client_confirm",
        "allow_delete": os.getenv("MYSQL_ALLOW_DELETE", "false").lower() == "true",
        "allow_ddl": False,
        "query_timeout": _env_int("MYSQL_MCP_QUERY_TIMEOUT", 60),
        "pool_size": _env_int("MYSQL_MCP_POOL_SIZE", 3),
    }


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


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
        _cache.setdefault("users", {})  # 旧版本配置没有 users 键：零迁移兼容
        if not _cache.get("databases"):
            env = _env_entry()
            if env:
                _cache = {"default_alias": "default", "users": {}, "databases": {"default": env}}
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


def validate_entry(alias: str, entry: dict, cfg: dict | None = None) -> None:
    """校验别名与条目必填项，非法时抛 ValueError（中文消息供管理页面展示）。

    账号来源两种模式（可混用）：
    - 引用模式：`read_user_ref`/`write_user_ref` 指向用户表中的名称；
    - 内联模式：`read_user`/`write_user` 自带账号密码（旧配置与 .env 回退沿用）。
    """
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
    users = (cfg or load_config()).get("users") or {}
    for role in ("read_user", "write_user"):
        ref = entry.get(f"{role}_ref") or entry.get(role, {}).get("ref")
        if ref:
            ref = str(ref).strip()
            if ref not in users:
                raise ValueError(f"{role}_ref 引用的用户「{ref}」不存在，请先在用户管理中创建")
            continue
        acct = entry.get(role) or {}
        if not acct.get("user"):
            raise ValueError(f"{role}.user 不能为空（或改为在用户管理中选择已存在的用户）")
    if entry.get("write_policy", "client_confirm") not in _VALID_POLICIES:
        raise ValueError("write_policy 只能是 client_confirm 或 elicitation_only")
    db_type = str(entry.get("db_type", "mysql") or "mysql").strip().lower()
    if db_type not in _VALID_DB_TYPES:
        raise ValueError("db_type 只能是 mysql 或 dameng")


# ---------------------------------------------------------------------------
# 用户表：名称即唯一键，账号/密码为凭证；数据库条目通过 read_user_ref /
# write_user_ref 引用。改名级联更新引用；被引用的用户禁止删除。
# ---------------------------------------------------------------------------

def validate_user(name: str, rec: dict) -> None:
    """校验用户记录，非法时抛 ValueError（中文消息）。"""
    if not name or not str(name).strip():
        raise ValueError("用户名称不能为空")
    if len(name) > USER_NAME_MAX:
        raise ValueError(f"用户名称过长（最多 {USER_NAME_MAX} 个字符）")
    if name != name.strip() or any(ord(c) < 32 for c in name):
        raise ValueError("用户名称不能包含首尾空白或控制字符")
    if not (rec or {}).get("user"):
        raise ValueError("账号不能为空")


def normalize_user(rec: dict) -> dict:
    """白名单归一化用户记录（密码可为空字符串）。"""
    rec = rec or {}
    return {"user": str(rec.get("user") or ""), "password": str(rec.get("password") or "")}


def list_users() -> dict[str, dict]:
    """返回用户表密钥（调用方不得原地修改）。"""
    return dict(load_config().get("users") or {})


def get_user(name: str) -> dict | None:
    rec = load_config().get("users") or {}
    return rec.get(name)


def user_references(name: str) -> list[str]:
    """返回引用了该用户的数据库别名列表（供删除前提示）。"""
    cfg = load_config()
    out = []
    for alias, entry in (cfg.get("databases") or {}).items():
        if any(entry.get(field) == name for field in _REF_ROLES):
            out.append(alias)
    return sorted(out)


def create_user(name: str, rec: dict) -> dict:
    """新建用户；重名抛 ValueError。"""
    validate_user(name, rec)
    cfg = load_config()
    if name in (cfg.get("users") or {}):
        raise ValueError(f"用户「{name}」已存在")
    normalized = normalize_user(rec)
    cfg.setdefault("users", {})[name] = normalized
    save_config(cfg)
    return normalized


def update_user(old_name: str, new_name: str, rec: dict) -> dict:
    """更新用户；支持改名并级联更新所有库引用。账号/密码留空表示不修改。"""
    cfg = load_config()
    users = cfg.get("users") or {}
    if old_name not in users:
        raise ValueError(f"用户「{old_name}」不存在")
    rec = rec or {}
    new_name = str(new_name or old_name).strip() or old_name
    updated = normalize_user({
        "user": rec.get("user") or users[old_name]["user"],
        "password": rec.get("password") or users[old_name]["password"],
    })
    validate_user(new_name, updated)
    if new_name != old_name and new_name in users:
        raise ValueError(f"用户「{new_name}」已存在")

    del users[old_name]
    users[new_name] = updated
    # 改名级联：同步所有数据库引用
    if new_name != old_name:
        for entry in (cfg.get("databases") or {}).values():
            for field in _REF_ROLES:
                if entry.get(field) == old_name:
                    entry[field] = new_name
    cfg["users"] = users
    save_config(cfg)
    return updated


def delete_user(name: str) -> None:
    """删除用户；被任何数据库引用时拒绝（需先改用其他用户）。"""
    cfg = load_config()
    if name not in (cfg.get("users") or {}):
        raise ValueError(f"用户「{name}」不存在")
    refs = user_references(name)
    if refs:
        raise ValueError(
            f"用户「{name}」正被 {len(refs)} 个数据库引用：{', '.join(refs)}。"
            "请先修改这些数据库的用户后再删除。")
    del cfg["users"][name]
    save_config(cfg)


def expand_entry(entry: dict, cfg: dict | None = None) -> dict:
    """把条目的 *_user_ref 展开为内联 read_user/write_user（就地修改并返回）。

    引用缺失时写入 entry["_account_error"]（中文提示），而非抛异常：
    配置被手工改坏时工具路径应给出可操作的指引，而不是连接超时。
    """
    if cfg is None:
        cfg = load_config()
    users = cfg.get("users") or {}
    for role in ("read", "write"):
        ref = entry.get(f"{role}_user_ref")
        if not ref:
            continue
        rec = users.get(ref)
        if rec is None:
            entry["_account_error"] = (
                f"数据库引用的用户「{ref}」不存在或已被删除，"
                "请在管理页面 /admin 的用户管理中修复该配置。")
            continue
        entry[f"{role}_user"] = {"user": rec.get("user", ""), "password": rec.get("password", "")}
    return entry


def migrate_inline_accounts() -> dict:
    """把各数据库的内联账号迁成用户记录，并把条目改为引用。

    按「账号名」去重建用户（只读/读写账号同名时合并），用户名称取账号名，
    冲突时追加序号。已引用用户的条目跳过。返回 {"created": [...], "updated": [...], "skipped": int}。
    """
    cfg = load_config()
    users = cfg.setdefault("users", {})
    created, updated = [], []
    skipped = 0
    for alias, entry in (cfg.get("databases") or {}).items():
        for role in ("read", "write"):
            if entry.get(f"{role}_user_ref"):
                continue
            acct = entry.get(f"{role}_user") or {}
            if not acct.get("user"):
                continue
            base = acct["user"]
            name = base
            n = 2
            while name in users and users[name] != normalize_user(acct):
                name = f"{base}__{n}"
                n += 1
            if name not in users:
                users[name] = normalize_user(acct)
                entry[f"{role}_user_ref"] = name
                created.append(name)
            else:
                entry[f"{role}_user_ref"] = name
                updated.append(name)
        if not entry.get("read_user_ref") and not entry.get("write_user_ref"):
            skipped += 1
    save_config(cfg)
    return {"created": created, "updated": updated, "skipped": skipped}


def normalize_entry(entry: dict) -> dict:
    """只保留已知字段并填默认值（未知字段丢弃）。"""
    def _acct(role):
        acct = entry.get(role) or {}
        return {"user": acct.get("user", ""), "password": acct.get("password", "")}

    def _ref(field):
        value = entry.get(field)
        return (str(value).strip() or None) if value else None

    def _ssh_int(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError("ssh 端口必须是整数") from None

    def _projects(value) -> list[str]:
        """项目名称列表：接受字符串（逗号/空白分隔）或字符串列表。"""
        if value is None:
            return []
        if isinstance(value, str):
            parts = [p.strip() for p in re.split(r"[,，\s]+", value)]
            return [p for p in parts if p]
        if isinstance(value, list):
            out: list[str] = []
            for p in value:
                s = str(p).strip()
                if s:
                    out.append(s)
            return out
        raise ValueError("projects 必须是字符串或字符串列表")

    def _nonneg_int(value, label: str) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label} 必须是整数") from None
        if n < 0:
            raise ValueError(f"{label} 不能为负数")
        return n

    ssh = entry.get("ssh") or {}
    db_type = str(entry.get("db_type", "mysql") or "mysql").strip().lower()
    if db_type not in _VALID_DB_TYPES:
        db_type = "mysql"
    return {
        "db_type": db_type,
        "host": entry.get("host", "localhost"),
        "port": int(entry.get("port", 3306)),
        "database": entry.get("database") or None,
        "charset": entry.get("charset") or "utf8mb4",
        "sql_mode": entry.get("sql_mode") or "TRADITIONAL",
        "connect_timeout": int(entry.get("connect_timeout", 10)),
        "query_timeout": _nonneg_int(entry.get("query_timeout", 60), "query_timeout"),
        "pool_size": _nonneg_int(entry.get("pool_size", 3), "pool_size"),
        "read_user": _acct("read_user"),
        "write_user": _acct("write_user"),
        "read_user_ref": _ref("read_user_ref"),
        "write_user_ref": _ref("write_user_ref"),
        "write_policy": entry.get("write_policy", "client_confirm"),
        "allow_delete": bool(entry.get("allow_delete", False)),
        "allow_ddl": bool(entry.get("allow_ddl", False)),
        "skip_confirm": bool(entry.get("skip_confirm", False)),
        "projects": _projects(entry.get("projects")),
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

    alias 优先按别名精确匹配；未命中时回退为按项目名称匹配
    （MCP 调用可传项目文件夹名，自动路由到绑定了该项目的数据库）。
    返回的 entry 是深拷贝，调用方原地修改不会污染缓存；
    read_user/write_user 引用已在返回前展开为实际账号密码。
    """
    cfg = load_config()
    dbs = cfg.get("databases", {})
    if alias:
        if alias in dbs:
            entry = json.loads(json.dumps(dbs[alias]))
            return (alias, expand_entry(entry, cfg))
        hit = _resolve_by_project(dbs, alias)
        return (hit[0], expand_entry(json.loads(json.dumps(hit[1])), cfg)) if hit else None
    default = cfg.get("default_alias")
    if default and default in dbs:
        entry = json.loads(json.dumps(dbs[default]))
        return default, expand_entry(entry, cfg)
    return None


def _resolve_by_project(dbs: dict, project: str) -> tuple[str, dict] | None:
    """按项目名称查找数据库条目；多个命中时取别名排序最小者（确定性）。"""
    matches = sorted(
        (a for a, e in dbs.items() if project in (e.get("projects") or [])),
    )
    if not matches:
        return None
    a = matches[0]
    return a, dbs[a]


def masked_config(cfg: dict) -> dict:
    """深拷贝并把非空密码替换为 '****'（覆盖用户表与数据库内联账号；原配置不动）。"""
    out = json.loads(json.dumps(cfg))
    for u in (out.get("users") or {}).values():
        if u.get("password"):
            u["password"] = "****"
    for e in out.get("databases", {}).values():
        for role in ("read_user", "write_user"):
            acct = e.get(role)
            if isinstance(acct, dict) and acct.get("password"):
                acct["password"] = "****"
    return out
