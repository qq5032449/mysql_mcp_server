"""管理页面 REST API：配置 CRUD、连接测试、默认别名设置、审计、健康检查。

访问控制：回环客户端直接放行；非回环需 ADMIN_TOKEN 令牌（未设置则仅回环）。
配置变更通过回调通知（server 模块注册以失效别名实例缓存）。
"""

import ipaddress
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import mysql.connector
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from mysql_mcp_server import audit, db_config

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
    try:
        db_config.validate_entry(alias, body)
        entry = db_config.normalize_entry(body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    cfg = db_config.load_config()
    if alias in cfg["databases"]:
        return JSONResponse({"error": f"别名 {alias} 已存在"}, status_code=409)
    cfg["databases"][alias] = entry
    if not cfg.get("default_alias"):
        cfg["default_alias"] = alias
    db_config.save_config(cfg)
    _notify_changed(alias)
    return JSONResponse({"ok": True, "default_alias": cfg["default_alias"]}, status_code=201)


async def update_database(request: Request):
    alias = request.path_params["alias"]
    body = await _body(request)
    cfg = db_config.load_config()
    old = cfg["databases"].get(alias)
    if old is None:
        return JSONResponse({"error": f"别名 {alias} 不存在"}, status_code=404)
    try:
        db_config.validate_entry(alias, body)
        new = db_config.normalize_entry(body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    # 密码为空表示不修改
    for role in ("read_user", "write_user"):
        if not new[role]["password"]:
            new[role]["password"] = old[role]["password"]
    cfg["databases"][alias] = new
    db_config.save_config(cfg)
    _notify_changed(alias)
    return JSONResponse({"ok": True})


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


async def test_connection(request: Request):
    alias = request.path_params["alias"]
    body = await _body(request)
    cfg = db_config.load_config()
    entry = cfg["databases"].get(alias)
    if entry is None:
        return JSONResponse({"error": f"别名 {alias} 不存在"}, status_code=404)
    role = "write" if body.get("as_write") else "read"
    acct = entry[f"{role}_user"]
    conn_cfg = {
        "host": entry["host"],
        "port": int(entry.get("port", 3306)),
        "user": acct["user"],
        "password": acct["password"],
        "connect_timeout": 5,
    }

    def _try() -> str | None:
        try:
            conn = mysql.connector.connect(**conn_cfg)
            conn.close()
            return None
        except Exception as e:
            return str(getattr(e, "msg", None) or e)

    error = await anyio.to_thread.run_sync(_try)
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


async def get_audit(request: Request):
    return JSONResponse(audit.list_entries())


async def get_health(request: Request):
    cfg = db_config.load_config()
    return JSONResponse({"status": "ok", "databases": len(cfg.get("databases", {}))})


class _GuardMiddleware(BaseHTTPMiddleware):
    """统一在 middleware 做访问控制，覆盖 API 路由与 /admin 静态挂载。

    - Host 校验对全部请求生效（防 DNS rebinding：只允许 IP/localhost）
    - 令牌校验仅对 /api/* 生效（静态页放行，由前端在 401 后引导输入令牌）
    """

    async def dispatch(self, request: Request, call_next):
        if not _host_allowed(request.headers.get("host")):
            return JSONResponse({"error": "forbidden: Host header not allowed"}, status_code=403)
        if request.url.path.startswith("/api"):
            if (g := _guard(request)):
                return g
        return await call_next(request)


def create_admin_app() -> Starlette:
    """独立的 admin Starlette 子应用（API + 静态页），由主服务挂载或测试直接使用。"""
    return Starlette(
        routes=[
            Route("/api/databases", get_databases, methods=["GET"]),
            Route("/api/databases", create_database, methods=["POST"]),
            Route("/api/databases/{alias}", update_database, methods=["PUT"]),
            Route("/api/databases/{alias}", delete_database, methods=["DELETE"]),
            Route("/api/databases/{alias}/test", test_connection, methods=["POST"]),
            Route("/api/settings", update_settings, methods=["PUT"]),
            Route("/api/audit", get_audit, methods=["GET"]),
            Route("/api/health", get_health, methods=["GET"]),
            Mount("/", app=StaticFiles(directory=STATIC_DIR, html=True, check_dir=False), name="admin"),
        ],
        middleware=[Middleware(_GuardMiddleware)],
    )
