"""管理页面 REST API：配置 CRUD、连接测试、默认别名设置、审计、错误日志、健康检查。

访问控制：回环客户端直接放行；非回环需 ADMIN_TOKEN 令牌（未设置则仅回环）。
配置变更通过回调通知（server 模块注册以失效别名实例缓存）。
"""

import ipaddress
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

import anyio
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from mysql_mcp_server import audit, db_config, db_drivers, error_log, ssh_tunnel

logger = logging.getLogger("mysql_mcp_server.admin_api")

STATIC_DIR = Path(__file__).parent / "static"

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
# Host 头白名单（防 DNS rebinding）；testserver/testserver.local 仅为 TestClient 默认 base_url 兼容
_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost", "testserver", "testserver.local"}

# 局域网访问令牌：设置后非回环客户端必须携带（X-Admin-Token 头或 admin_token 参数）
_ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

_on_change_callbacks = []


def register_on_change(cb) -> None:
    """注册配置变更回调 cb(alias: str)；同一回调重复注册只保留一份。"""
    if cb not in _on_change_callbacks:
        _on_change_callbacks.append(cb)


def _notify_changed(alias: str) -> None:
    for cb in _on_change_callbacks:
        try:
            cb(alias)
        except Exception:
            logger.warning("on-change callback failed", exc_info=True)


def _is_loopback(ip: str) -> bool:
    if ip.startswith("::ffff:"):  # IPv4-mapped IPv6
        ip = ip[7:]
    return ip in _LOOPBACK


def _guard(request: Request) -> JSONResponse | None:
    """回环客户端直接放行；非回环需 ADMIN_TOKEN 令牌（未配置令牌则保持仅回环）。"""
    ip = request.client.host if request.client else ""
    if _is_loopback(ip):
        return None
    if not _ADMIN_TOKEN:
        return JSONResponse({"error": "forbidden: admin API is loopback-only (set ADMIN_TOKEN for LAN access)"}, status_code=403)
    supplied = request.headers.get("x-admin-token") or request.query_params.get("admin_token")
    if supplied != _ADMIN_TOKEN:
        return JSONResponse({"error": "unauthorized: invalid or missing admin token"}, status_code=401)
    return None


def _host_allowed(host_header: str | None) -> bool:
    """Host 头必须是 IP 地址或 localhost（防 DNS rebinding 域名攻击）；缺失或解析为空时放行。"""
    if not host_header:
        return True
    hostname = urlsplit("//" + host_header).hostname
    if not hostname:
        return True
    h = hostname.lower()
    if h in _ALLOWED_HOSTS:
        return True
    try:
        ipaddress.ip_address(h)
        return True  # 任意 IP 直连（含局域网 IP）
    except ValueError:
        return False  # 域名形式一律拒绝（DNS rebinding 防护）


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def get_databases(request: Request):
    return JSONResponse(db_config.masked_config(db_config.load_config()))


async def create_database(request: Request):
    body = await _body(request)
    alias = str(body.get("alias", "")).strip()
    cfg = db_config.load_config()
    try:
        db_config.validate_entry(alias, body, cfg)
        entry = db_config.normalize_entry(body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if alias in cfg["databases"]:
        return JSONResponse({"error": f"别名 {alias} 已存在"}, status_code=409)
    cfg["databases"][alias] = entry
    if not cfg.get("default_alias"):
        cfg["default_alias"] = alias
    db_config.save_config(cfg)
    _notify_changed(alias)
    # 保存后自动校验（不阻断）：库名写错或库不存在时立即提示，避免日后查询才暴露
    warning = await verify_entry_saved(entry)
    resp = {"ok": True, "default_alias": cfg["default_alias"]}
    if warning:
        resp["warning"] = warning
    return JSONResponse(resp, status_code=201)


async def update_database(request: Request):
    alias = request.path_params["alias"]
    body = await _body(request)
    cfg = db_config.load_config()
    old = cfg["databases"].get(alias)
    if old is None:
        return JSONResponse({"error": f"别名 {alias} 不存在"}, status_code=404)
    try:
        db_config.validate_entry(alias, body, cfg)
        new = db_config.normalize_entry(body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    # 内联账号模式下密码为空表示不修改；引用模式密码来自用户表，无需处理
    for role in ("read_user", "write_user"):
        if not new.get(f"{role}_ref") and not new[role]["password"]:
            new[role]["password"] = old[role]["password"]
    cfg["databases"][alias] = new
    db_config.save_config(cfg)
    _notify_changed(alias)
    warning = await verify_entry_saved(new)
    resp = {"ok": True}
    if warning:
        resp["warning"] = warning
    return JSONResponse(resp)


async def delete_database(request: Request):
    alias = request.path_params["alias"]
    cfg = db_config.load_config()
    if alias not in cfg["databases"]:
        return JSONResponse({"error": f"别名 {alias} 不存在"}, status_code=404)
    del cfg["databases"][alias]
    if cfg.get("default_alias") == alias:
        remaining = sorted(cfg["databases"])
        cfg["default_alias"] = remaining[0] if remaining else None
    db_config.save_config(cfg)
    _notify_changed(alias)
    return JSONResponse({"ok": True, "default_alias": cfg["default_alias"]})


async def export_databases(request: Request):
    """导出完整配置（含密码，用于备份/迁移；令牌或回环保护，勿外传）。"""
    return JSONResponse(db_config.load_config())


async def import_databases(request: Request):
    """整份导入配置：校验 users 与全部库条目（含引用完整性）后原子替换。

    导入内容应来自 export_databases。
    """
    body = await _body(request)
    if not isinstance(body.get("databases"), dict) or not body["databases"]:
        return JSONResponse({"error": "导入内容必须包含非空的 databases 对象"}, status_code=400)
    users_raw = body.get("users") or {}
    if not isinstance(users_raw, dict):
        return JSONResponse({"error": "导入内容 users 必须是对象"}, status_code=400)
    users: dict[str, dict] = {}
    for name, rec in users_raw.items():
        name = str(name).strip()
        try:
            db_config.validate_user(name, rec or {})
            users[name] = db_config.normalize_user(rec or {})
        except ValueError as e:
            return JSONResponse({"error": f"用户 {name} 校验失败：{e}"}, status_code=400)
    # 引用完整性：导入的配置必须自洽（用户已在本批 users 中或内联账号有效）
    for alias, entry in body["databases"].items():
        try:
            db_config.validate_entry(str(alias).strip(), entry or {}, {"users": users})
        except ValueError as e:
            return JSONResponse({"error": f"条目 {alias} 校验失败：{e}"}, status_code=400)
    normalized: dict[str, dict] = {}
    for alias, entry in body["databases"].items():
        alias = str(alias).strip()
        normalized[alias] = db_config.normalize_entry(entry or {})
    default_alias = str(body.get("default_alias") or "").strip() or None
    if default_alias and default_alias not in normalized:
        return JSONResponse({"error": f"default_alias {default_alias} 不在导入条目中"}, status_code=400)
    cfg = {"default_alias": default_alias or sorted(normalized)[0],
           "users": users, "databases": normalized}
    db_config.save_config(cfg)
    _notify_changed(None)
    return JSONResponse({"ok": True, "default_alias": cfg["default_alias"],
                         "count": len(normalized), "users": len(users)})


def probe_entry(entry: dict, role: str = "read") -> str | None:
    """连接探测：返回 None 表示连通且库/模式可见，否则返回中文错误说明。

    先验证实例连通与账号认证（连接不带库名），再单独校验库（达梦为模式）存在且可见——
    直接带库名连接只能得到驱动的 "Unknown database"，无法区分是实例不通还是库写错。
    供「测试连接」与保存时的自动校验共用。
    """
    if db_drivers.entry_db_type(entry) == db_drivers.DAMENG:
        try:
            import dmPython  # noqa: F401
        except ImportError:
            return "未安装 dmPython 驱动：pip install dmPython"
        with ssh_tunnel.maybe_ssh_tunnel_for(entry) as (host, port):
            probe = {**entry, "database": None}
            try:
                conn = db_drivers.connect_entry(probe, role, host, port)
            except Exception as e:
                return db_drivers.error_message(entry, e)
            try:
                schema = entry.get("database")
                if not schema:
                    return None
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT USERNAME FROM ALL_USERS WHERE UPPER(USERNAME) = UPPER("
                        f"{db_drivers.qstr(schema)})"
                    )
                    if cursor.fetchone() is None:
                        return (f"模式（schema）'{schema}' 不存在或当前账号无权访问"
                                "（实例连接与账号认证均正常）")
                return None
            except Exception as e:
                return db_drivers.error_message(entry, e)
            finally:
                conn.close()

    import mysql.connector
    db_name = entry.get("database")
    with ssh_tunnel.maybe_ssh_tunnel_for(entry) as (host, port):
        conn_cfg = db_drivers.build_config(entry, role, host, port)
        conn_cfg.pop("database", None)
        conn_cfg["connect_timeout"] = 5
        try:
            conn = mysql.connector.connect(**conn_cfg)
        except Exception as e:
            return str(getattr(e, "msg", None) or e)
        try:
            if not db_name:
                return None  # 多数据库模式：无库名校验
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA"
                    " WHERE SCHEMA_NAME = %s",
                    (db_name,),
                )
                if cursor.fetchone() is None:
                    return (f"数据库 '{db_name}' 不存在或当前账号无权访问"
                            "（实例连接与账号认证均正常）")
            return None
        except Exception as e:
            return str(getattr(e, "msg", None) or e)
        finally:
            conn.close()


def verify_on_save() -> bool:
    """保存数据库时是否自动校验连通性与库存在（默认开）；MYSQL_MCP_VERIFY_ON_SAVE=false 关闭。"""
    return os.getenv("MYSQL_MCP_VERIFY_ON_SAVE", "true").strip().lower() != "false"


async def verify_entry_saved(entry: dict) -> str | None:
    """保存后的自动校验：返回 warning 文案（None 表示无问题）。

    只做提示不阻断保存——库暂时不可达、或先建配置后建库都是合理场景。
    """
    if not verify_on_save():
        return None
    # 引用模式必须先把用户账号展开，否则探测拿到的是空账号
    probe = dict(entry)
    db_config.expand_entry(probe, db_config.load_config())
    if probe.get("_account_error"):
        return f"保存成功，但自动校验未通过：{probe['_account_error']}"
    probe.pop("_account_error", None)
    try:
        error = await anyio.to_thread.run_sync(lambda: probe_entry(probe, "read"))
    except Exception as e:
        return f"保存成功，但自动校验未能完成：{e}"
    if error is None:
        return None
    return f"保存成功，但自动校验未通过：{error}"


async def test_connection(request: Request):
    alias = request.path_params["alias"]
    body = await _body(request)
    cfg = db_config.load_config()
    entry = cfg["databases"].get(alias)
    if entry is None:
        return JSONResponse({"error": f"别名 {alias} 不存在"}, status_code=404)
    # 引用模式：展开为用户表中的账号密码；悬空引用直接报可操作的提示
    entry = dict(entry)
    db_config.expand_entry(entry, cfg)
    if entry.get("_account_error"):
        return JSONResponse({"ok": False, "error": entry["_account_error"], "role": "read"}, status_code=200)
    role = "write" if body.get("as_write") else "read"
    error = await anyio.to_thread.run_sync(lambda: probe_entry(entry, role))
    if error is None:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": error, "role": role})


async def update_settings(request: Request):
    body = await _body(request)
    alias = str(body.get("default_alias", "")).strip()
    cfg = db_config.load_config()
    if alias not in cfg["databases"]:
        return JSONResponse({"error": f"别名 {alias} 不存在"}, status_code=400)
    cfg["default_alias"] = alias
    db_config.save_config(cfg)
    _notify_changed(alias)
    return JSONResponse({"ok": True, "default_alias": alias})


# ---------------------------------------------------------------------------
# 用户管理：名称即唯一键，账号/密码为凭证；数据库条目通过 *_user_ref 引用
# ---------------------------------------------------------------------------

async def list_users(request: Request):
    """用户列表（密码脱敏；附带被引用情况，供前端展示与删除前判断）。"""
    users = db_config.list_users()
    out = []
    for name, rec in users.items():
        out.append({"name": name, "user": rec.get("user", ""),
                    "password": "****" if rec.get("password") else "",
                    "references": db_config.user_references(name)})
    out.sort(key=lambda u: u["name"])
    return JSONResponse({"users": out})


async def create_user(request: Request):
    body = await _body(request)
    name = str(body.get("name", "")).strip()
    try:
        db_config.create_user(name, body)
    except ValueError as e:
        status = 409 if "已存在" in str(e) else 400
        return JSONResponse({"error": str(e)}, status_code=status)
    _notify_changed(None)
    return JSONResponse({"ok": True, "name": name}, status_code=201)


async def update_user(request: Request):
    name = request.path_params["name"]
    body = await _body(request)
    new_name = str(body.get("name", name)).strip()
    try:
        db_config.update_user(name, new_name, body)
    except ValueError as e:
        status = 409 if "已存在" in str(e) else (404 if "不存在" in str(e) else 400)
        return JSONResponse({"error": str(e)}, status_code=status)
    _notify_changed(None)
    return JSONResponse({"ok": True, "name": new_name, "renamed": new_name != name})


async def delete_user(request: Request):
    name = request.path_params["name"]
    force = request.query_params.get("force") == "true"
    refs = db_config.user_references(name)
    if refs and not force:
        return JSONResponse({"error": f"用户「{name}」正被 {len(refs)} 个数据库引用：{', '.join(refs)}",
                             "references": refs}, status_code=409)
    try:
        if refs:
            # 强制删除：清空所有引用
            cfg = db_config.load_config()
            for entry in (cfg.get("databases") or {}).values():
                for field in ("read_user_ref", "write_user_ref"):
                    if entry.get(field) == name:
                        entry[field] = None
            cfg["users"].pop(name, None)
            db_config.save_config(cfg)
        else:
            db_config.delete_user(name)
    except ValueError as e:
        status = 404 if "不存在" in str(e) else 400
        return JSONResponse({"error": str(e)}, status_code=status)
    _notify_changed(None)
    return JSONResponse({"ok": True, "force_cleared": len(refs)})


async def migrate_inline_users(request: Request):
    """把各库的内联账号迁成用户记录并改为引用。"""
    try:
        result = db_config.migrate_inline_accounts()
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    _notify_changed(None)
    return JSONResponse({"ok": True, **result})


async def get_audit(request: Request):
    return JSONResponse(audit.list_entries())


async def get_errors(request: Request):
    """持久化执行错误查询：?limit=100&alias=db1，最新在前。"""
    params = request.query_params
    return JSONResponse(error_log.query_errors(
        limit=params.get("limit", 100), alias=params.get("alias") or None))


async def get_health(request: Request):
    cfg = db_config.load_config()
    return JSONResponse({"status": "ok",
                          "databases": len(cfg.get("databases", {})),
                          "users": len(cfg.get("users") or {})})


class _GuardMiddleware(BaseHTTPMiddleware):
    """统一在 middleware 做访问控制，覆盖 API 路由与 /admin 静态挂载。

    - Host 校验对全部请求生效（防 DNS rebinding：只允许 IP/localhost）
    - 令牌校验仅对 /api/* 生效（静态页放行，由前端在 401 后引导输入令牌）
    """

    async def dispatch(self, request: Request, call_next):
        if not _host_allowed(request.headers.get("host")):
            return JSONResponse({"error": "forbidden: Host header not allowed"}, status_code=403)
        # Mount 挂载时 url.path 含挂载前缀（如 /admin/api/health），剥掉 root_path 后再判断
        root_path = request.scope.get("root_path", "")
        path = request.url.path
        if root_path and path.startswith(root_path):
            path = path[len(root_path):]
        if path.startswith("/api"):
            if (g := _guard(request)):
                return g
        return await call_next(request)


def create_admin_app() -> Starlette:
    """独立的 admin Starlette 子应用（API + 静态页），由主服务挂载或测试直接使用。"""
    return Starlette(
        routes=[
            Route("/api/databases", get_databases, methods=["GET"]),
            Route("/api/databases", create_database, methods=["POST"]),
            Route("/api/databases/export", export_databases, methods=["GET"]),
            Route("/api/databases/import", import_databases, methods=["POST"]),
            Route("/api/databases/{alias}", update_database, methods=["PUT"]),
            Route("/api/databases/{alias}", delete_database, methods=["DELETE"]),
            Route("/api/databases/{alias}/test", test_connection, methods=["POST"]),
            Route("/api/settings", update_settings, methods=["PUT"]),
            Route("/api/users", list_users, methods=["GET"]),
            Route("/api/users", create_user, methods=["POST"]),
            Route("/api/users/migrate", migrate_inline_users, methods=["POST"]),
            Route("/api/users/{name}", update_user, methods=["PUT"]),
            Route("/api/users/{name}", delete_user, methods=["DELETE"]),
            Route("/api/audit", get_audit, methods=["GET"]),
            Route("/api/errors", get_errors, methods=["GET"]),
            Route("/api/health", get_health, methods=["GET"]),
            Mount("/", app=StaticFiles(directory=STATIC_DIR, html=True, check_dir=False), name="admin"),
        ],
        middleware=[Middleware(_GuardMiddleware)],
    )
