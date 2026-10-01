import asyncio
import json
import logging
import os
import re
import socket
import time
import traceback
import urllib.parse
from typing import List, Optional, Tuple, Any

import anyio
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import (
    Resource, Tool, TextContent, ToolAnnotations, ResourceTemplate,
    Prompt, PromptArgument, PromptMessage, GetPromptResult,
)
from pydantic import AnyUrl
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route

from mysql_mcp_server import admin_api, audit, conn_pool, db_config, db_drivers, error_log
from mysql_mcp_server.ssh_tunnel import maybe_ssh_tunnel_for
from mysql_mcp_server.sql_classify import (
    DDL_PREFIXES,
    classify,
    cte_main_keyword,
    first_keyword,
    has_top_level_where,
    top_level_semicolon_count,
)

# Load environment variables from .env file if it exists.
# This allows for easy local configuration of database and SSH credentials.
load_dotenv()

# Configure logging to provide visibility into server operations.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mysql_mcp_server")

# ---------------------------------------------------------------------------
# 协议兼容：部分客户端（如 TRAE）SSE 断线重连后不在新会话上重走 initialize
# 握手，直接发送 tools/call 等请求。SDK 的 ServerSession 会因"未完成初始化"
# 拒绝请求（客户端表现为 Invalid request parameters）。这里放宽：未初始化的
# 会话收到首条消息时自动视为已初始化。
# 安全性：跳过握手的会话拿不到 client_params，elicit_confirm 探测不到
# elicitation 能力，写操作照常按 write_policy 降级走令牌二次确认，
# 删除类仍受 allow_delete 开关约束，与无 elicitation 的普通客户端一致。
# ---------------------------------------------------------------------------
def _patch_session_auto_init() -> None:
    try:
        from mcp.server.session import InitializationState, ServerSession
        from mcp.types import InitializeRequest
    except ImportError:
        logger.warning("mcp.server.session import failed; auto-init patch skipped")
        return
    if getattr(ServerSession, "_auto_init_patched", False):
        return

    ServerSession._received_request_original = ServerSession._received_request
    ServerSession._received_notification_original = ServerSession._received_notification

    async def _received_request(self, responder):
        if (not isinstance(responder.request.root, InitializeRequest)
                and self._initialization_state != InitializationState.Initialized):
            logger.warning(
                "Session not initialized but received %s; auto-initializing "
                "(client skipped initialize handshake)",
                type(responder.request.root).__name__)
            self._initialization_state = InitializationState.Initialized
        return await self._received_request_original(responder)

    async def _received_notification(self, notification):
        if self._initialization_state != InitializationState.Initialized:
            self._initialization_state = InitializationState.Initialized
        return await self._received_notification_original(notification)

    ServerSession._received_request = _received_request
    ServerSession._received_notification = _received_notification
    ServerSession._auto_init_patched = True
    logger.info("ServerSession auto-init tolerance patch applied")


_patch_session_auto_init()

# System databases that are typically filtered out from resource listings.
SYSTEM_DATABASES = {'information_schema', 'mysql', 'performance_schema', 'sys'}

def validate_identifier(name: str) -> str:
    """
    Validate a MySQL identifier (table or database name) to prevent SQL injection.
    Only allows alphanumeric characters, underscores, and dollar signs.
    """
    if not re.match(r'^[a-zA-Z0-9_$]+$', name):
        raise ValueError(f"Invalid identifier '{name}': only alphanumeric, underscore, and $ are allowed")
    return name

def parse_table_arg(name: str | None) -> Tuple[Optional[str], str]:
    """Split an optional 'database.table' argument into (db, table) parts, validating each."""
    if not name or not isinstance(name, str):
        raise ValueError("table_name 参数必填，请提供表名（可用 database.table 形式）。")
    if "." in name:
        db, tbl = name.split(".", 1)
        return validate_identifier(db), validate_identifier(tbl)
    return None, validate_identifier(name)

# ---------------------------------------------------------------------------
# 别名/双账号支持：从 databases.json 条目构造连接配置并执行
# ---------------------------------------------------------------------------

def build_connector_config(entry: dict, role: str, host=None, port=None) -> dict:
    """从别名条目构造驱动原生连接配置；role='read'|'write' 选择对应账号。

    保留原函数名兼容旧测试与调用方，内部委托 db_drivers.build_config。
    """
    return db_drivers.build_config(entry, role, host, port)


async def elicit_confirm(alias: str, kind_label: str, query: str, timeout: float = 30.0,
                         warning: str = "") -> str | None:
    """发起服务端 SQL 确认（MCP elicitation）。

    返回 'accept' / 'decline' / 'cancel'；客户端不支持或超时返回 None（由调用方降级）。
    """
    from mcp.server.lowlevel.server import request_ctx
    try:
        ctx = request_ctx.get()
    except LookupError:
        return None
    session = ctx.session
    params = session.client_params
    caps = params.capabilities if params else None
    if not caps or not caps.elicitation:
        # 诊断日志：确认客户端握手时未声明 elicitation 能力（写操作将按策略降级）
        caps_summary = caps.model_dump(exclude_none=True) if caps else None
        logger.info(f"Client lacks elicitation capability; write will degrade by write_policy. client_caps={caps_summary}")
        return None
    try:
        with anyio.move_on_after(timeout):
            result = await session.elicit(
                message=f"确认在数据库别名 [{alias}] 上执行{kind_label}操作：\n\n{query}{warning}",
                requestedSchema={"type": "object", "properties": {}},
            )
            return result.action
    except Exception as e:
        logger.warning(f"Elicitation failed: {e}")
        return None
    return None  # 超时


_KIND_LABELS = {"write": "写", "delete": "删除类"}


def _read_audit_enabled() -> bool:
    """读操作是否也记审计（默认关）；MYSQL_MCP_AUDIT_READ=true 开启。"""
    return os.getenv("MYSQL_MCP_AUDIT_READ", "false").strip().lower() == "true"


def _where_warning(query: str) -> str:
    """UPDATE/DELETE 顶层缺 WHERE 时生成高危提示（附加到确认文案，不改 SQL）。"""
    kw = first_keyword(query)
    if kw == "WITH":
        kw = cte_main_keyword(query)
    if kw in ("UPDATE", "DELETE") and not has_top_level_where(query):
        return "\n\n⚠️ 警告：该语句未检测到 WHERE 条件，将影响全表所有行！"
    return ""

# ---------------------------------------------------------------------------
# 聊天内二次确认：客户端不支持 elicitation 时，签发一次性令牌，
# AI 在聊天中向用户展示 SQL 征求同意后，携带令牌重新调用才执行。
# ---------------------------------------------------------------------------

_TOKEN_TTL_SECONDS = 300  # 令牌有效期（5 分钟）
_TOKEN_MIN_DELAY_SECONDS = 6.0  # 冷静期：签发后 6 秒内不可使用（防止跳过用户确认）

_pending_tokens: dict[str, dict] = {}  # token -> {alias, sql, expires, not_before}
_token_lock = anyio.Lock()


def _now() -> float:
    return time.monotonic()


async def _issue_token(alias: str, query: str) -> str:
    """签发一次性确认令牌（绑定别名与 SQL），并清理过期令牌。"""
    import secrets
    token = secrets.token_urlsafe(16)
    async with _token_lock:
        expired = [t for t, v in _pending_tokens.items() if v["expires"] < _now()]
        for t in expired:
            del _pending_tokens[t]
        _pending_tokens[token] = {
            "alias": alias,
            "sql": query,
            "expires": _now() + _TOKEN_TTL_SECONDS,
            "not_before": _now() + _TOKEN_MIN_DELAY_SECONDS,
        }
    return token


async def _consume_token(token: str, alias: str, query: str) -> str:
    """校验并消费令牌。

    返回 'ok'（有效并已消费）| 'too_early'（冷静期内使用，令牌作废）
    | 'invalid'（不存在/过期/不匹配）。
    """
    async with _token_lock:
        info = _pending_tokens.get(token)
        if info is None or info["expires"] < _now():
            _pending_tokens.pop(token, None)
            return "invalid"
        if info["alias"] != alias or info["sql"] != query:
            return "invalid"  # 不匹配不消费，等待正确调用或过期
        if _now() < info["not_before"]:
            # 冷静期内回传：不可能已完成用户确认，令牌作废强制重走流程
            del _pending_tokens[token]
            return "too_early"
        del _pending_tokens[token]
        return "ok"


async def execute_sql_entry(alias: str, entry: dict, query: str, confirm_token: str | None = None) -> list[TextContent]:
    """三级判定 + 权限门控 + 双账号执行 + 确认（elicitation 弹窗或聊天内令牌二次确认）。

    执行结果状态（成功/失败、耗时、影响行数）由 run_query_entry 通过 audit_ctx 记入审计。
    """
    kind = classify(query)
    kw = first_keyword(query)
    if kw == "WITH":
        kw = cte_main_keyword(query)
    sql_type = kw or kind.upper()

    if kind == "read":
        audit_ctx = {"channel": "read"} if _read_audit_enabled() else None
        return await run_query_entry(entry, query, "read",
                                     alias=alias, tool="execute_sql", sql_type=sql_type,
                                     audit_ctx=audit_ctx)

    if kind == "delete" and not entry.get("allow_delete"):
        audit.record(alias, sql_type, "-", query, status="rejected_delete_disabled")
        return [TextContent(type="text", text="该别名未开启删除权限，请在管理页面开启后重试。")]

    if kw in DDL_PREFIXES and not entry.get("allow_ddl"):
        audit.record(alias, sql_type, "-", query, status="rejected_ddl_disabled")
        return [TextContent(type="text", text=(
            "该别名未开启 DDL 权限（allow_ddl），CREATE/ALTER/RENAME/GRANT/REVOKE 类语句已被拒绝。"
            "如确需执行，请在管理页面开启该别名的「允许 DDL」。"))]

    warning = _where_warning(query)

    # skip_confirm 开启：写操作（不含删除类）免二次确认直接执行
    if kind == "write" and entry.get("skip_confirm"):
        return await run_query_entry(entry, query, "write",
                                     alias=alias, tool="execute_sql", sql_type=sql_type,
                                     audit_ctx={"channel": "skip_confirm"})

    # 携带令牌：校验通过则直接用操作账号执行（聊天内二次确认的第二步）
    if confirm_token:
        verdict = await _consume_token(confirm_token, alias, query)
        if verdict == "ok":
            return await run_query_entry(entry, query, "write",
                                         alias=alias, tool="execute_sql", sql_type=sql_type,
                                         audit_ctx={"channel": "token"})
        if verdict == "too_early":
            audit.record(alias, sql_type, "token", query, status="token_too_early")
            return [TextContent(type="text", text=(
                "确认令牌在签发后 6 秒内不可使用——检测到未经过用户确认即尝试执行，该令牌已作废。"
                "必须使用 AskUserQuestion 工具向用户完整展示 SQL 并获得明确授权；"
                "用户同意后，重新调用（不带 confirm_token）获取新令牌，"
                "待用户确认完成后再携带新令牌执行。"))]
        audit.record(alias, sql_type, "token", query, status="invalid_token")
        return [TextContent(type="text", text=(
            "确认令牌无效、已过期或与本条 SQL 不匹配。"
            "请重新发起（不带 confirm_token 调用）以获取新令牌，并再次向用户确认。"))]

    label = _KIND_LABELS.get(kind, "写")
    action = await elicit_confirm(alias, label, query, warning=warning)

    if action == "accept":
        return await run_query_entry(entry, query, "write",
                                     alias=alias, tool="execute_sql", sql_type=sql_type,
                                     audit_ctx={"channel": "elicitation"})
    if action in ("decline", "cancel"):
        audit.record(alias, sql_type, "elicitation", query, status=f"user_{action}")
        return [TextContent(type="text", text="用户已拒绝执行该 SQL。")]

    # 客户端不支持 elicitation → 按别名策略降级
    policy = entry.get("write_policy", "client_confirm")
    if policy == "elicitation_only":
        audit.record(alias, sql_type, "-", query, status="blocked_policy")
        return [TextContent(type="text", text=(
            "当前客户端不支持服务端确认（elicitation），"
            "且该别名策略为 elicitation_only，写操作已被拒绝。"))]
    # client_confirm：签发一次性令牌，要求聊天内二次确认
    token = await _issue_token(alias, query)
    audit.record(alias, sql_type, "token", query, status="pending_token")
    return [TextContent(type="text", text=(
        f"⚠️ 该{label}操作需要使用AskUserQuestion 工具弹出确认窗口，用户确认后才会继续执行。请向用户完整展示以下 SQL 并明确征求同意；"
        f"用户同意后，使用相同 query 并携带 confirm_token={token} 重新调用 execute_sql。"
        f"令牌 5 分钟内有效、一次性且仅对本条 SQL 有效。{warning}\n\n{query}"))]

# Create the MCP Server instance.
app = Server("mysql_mcp_server")


async def list_resources_impl(alias: str | None = None) -> list[Resource]:
    """
    Lists available MySQL tables (or databases if no default database is configured) as resources.
    This allows AI agents to discover what data is available.
    未配置任何数据库时返回空列表。
    """
    resolved = db_config.resolve(alias)
    if resolved is None:
        return []
    log_alias, entry = resolved

    def _sync_list():
        with maybe_ssh_tunnel_for(entry) as (host, port):
            try:
                with conn_pool.get_connection(entry, "read", host, port) as conn:
                    with conn.cursor() as cursor:
                        if not entry.get("database"):
                            # Multi-database mode: list available databases/schemas.
                            cursor.execute(db_drivers.list_schemas_sql(entry))
                            databases = cursor.fetchall()
                            label = "schema" if db_drivers.entry_db_type(entry) == db_drivers.DAMENG else "database"
                            return [
                                Resource(
                                    uri=f"mysql://database/{db[0]}",
                                    name=f"database_{db[0]}",
                                    mimeType="text/plain",
                                    description=f"{label}: {db[0]}"
                                )
                                for db in databases if db[0] not in SYSTEM_DATABASES
                            ]
                        else:
                            # Single-database mode: list tables in the configured database.
                            cursor.execute(db_drivers.list_tables_sql(entry))
                            tables = cursor.fetchall()
                            resources = []
                            for table in tables:
                                resources.append(
                                    Resource(
                                        uri=f"mysql://{table[0]}/data",
                                        name=f"table_{table[0]}",
                                        mimeType="text/plain",
                                        description=f"Data in table: {table[0]}"
                                    )
                                )
                            return resources
            except Exception as e:
                error_msg = db_drivers.error_message(entry, e)
                logger.error(f"Failed to list resources: {error_msg}")
                error_log.record(log_alias, "list_resources",
                                 db_drivers.list_tables_sql(entry)
                                 if entry.get("database") else db_drivers.list_schemas_sql(entry),
                                 error_msg, sql_type="-", role="read")
                return []

    return await anyio.to_thread.run_sync(_sync_list)


@app.list_resources()
async def list_resources() -> list[Resource]:
    """模块级默认实例（无别名）：转发到 list_resources_impl。"""
    return await list_resources_impl(None)

@app.list_resource_templates()
async def list_resource_templates() -> list[ResourceTemplate]:
    """
    Returns available resource templates. Currently returns an empty list,
    but implemented for better compatibility with tools like Visual Studio Code.
    """
    return []





async def read_resource_impl(alias: str | None = None, uri: AnyUrl = None) -> str:
    """
    Reads the content of a specific table or lists tables within a database based on the provided URI.
    资源读取固定使用 read 角色；未配置任何数据库时抛 RuntimeError。
    """
    resolved = db_config.resolve(alias)
    if resolved is None:
        raise RuntimeError("No database configured")
    log_alias, entry = resolved

    def _sync_read():
        with maybe_ssh_tunnel_for(entry) as (host, port):
            uri_str = str(uri)
            if not uri_str.startswith("mysql://"):
                raise ValueError(f"Invalid URI scheme: {uri_str}")

            parts = uri_str[8:].split('/')

            # Handle requests to list tables in a specific database.
            if len(parts) >= 2 and parts[0] == "database":
                db_name = validate_identifier(parts[1])
                try:
                    with conn_pool.get_connection(entry, "read", host, port) as conn:
                        with conn.cursor() as cursor:
                            if db_drivers.entry_db_type(entry) == db_drivers.DAMENG:
                                cursor.execute(
                                    "SELECT TABLE_NAME FROM ALL_TABLES WHERE "
                                    f"UPPER(OWNER) = UPPER({db_drivers.qstr(db_name)}) "
                                    "ORDER BY TABLE_NAME"
                                )
                            else:
                                cursor.execute(f"USE `{db_name}`")
                                cursor.execute("SHOW TABLES")
                            tables = cursor.fetchall()
                            result = [f"Tables in database '{db_name}':"]
                            result.extend([table[0] for table in tables])
                            return "\n".join(result)
                except Exception as e:
                    error_msg = db_drivers.error_message(entry, e)
                    error_log.record(log_alias, "read_resource", str(uri),
                                     error_msg, sql_type="-", role="read")
                    raise RuntimeError(f"Database error: {error_msg}")

            # Handle requests to read data from a specific table.
            table = validate_identifier(parts[0])
            try:
                with conn_pool.get_connection(entry, "read", host, port) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(db_drivers.sample_sql(None, table, db_drivers.table_sample_default(entry), entry))
                        columns = [desc[0] for desc in cursor.description]
                        rows = cursor.fetchall()
                        # Format output as simple CSV-like text.
                        result = [",".join("" if v is None else str(v) for v in row) for row in rows]
                        return "\n".join([",".join(columns)] + result)
            except Exception as e:
                error_msg = db_drivers.error_message(entry, e)
                error_log.record(log_alias, "read_resource", str(uri),
                                 error_msg, sql_type="-", role="read")
                raise RuntimeError(f"Database error: {error_msg}")

    return await anyio.to_thread.run_sync(_sync_read)


@app.read_resource()
async def read_resource(uri: AnyUrl) -> str:
    """模块级默认实例（无别名）：转发到 read_resource_impl。"""
    return await read_resource_impl(None, uri)


# ---------------------------------------------------------------------------
# 别名 Server 工厂：SSE 模式下每个别名一个 MCP Server 实例
# ---------------------------------------------------------------------------

_server_registry: dict[str, Server] = {}


def invalidate_alias(alias: str | None = None) -> None:
    """配置变更后使别名 Server 缓存失效（admin_api 回调触发；None 清空全部）。"""
    if alias is None:
        _server_registry.clear()
    else:
        _server_registry.pop(alias, None)


def create_alias_server(alias: str) -> Server:
    """创建绑定指定别名的 MCP Server 实例（SSE 每别名一实例）。"""
    s = Server("mysql_mcp_server")
    s.list_tools()(list_tools)
    s.list_resource_templates()(list_resource_templates)
    s.list_prompts()(list_prompts)
    s.get_prompt()(get_prompt)

    async def _list_resources() -> list[Resource]:
        return await list_resources_impl(alias)

    async def _read_resource(uri: AnyUrl) -> str:
        return await read_resource_impl(alias, uri)

    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        return await call_tool_impl(name, arguments, alias)

    s.list_resources()(_list_resources)
    s.read_resource()(_read_resource)
    s.call_tool(validate_input=False)(_call_tool)
    return s


def _effective_alias(connection_alias: str | None, arguments: dict) -> str | None:
    p = arguments.get("alias")
    if isinstance(p, str) and p.strip():
        return p.strip()
    return connection_alias


def _unknown_alias_hint(alias: str) -> str:
    cfg = db_config.load_config()
    dbs = cfg.get("databases", {})
    if not dbs:
        return (f"别名或项目 '{alias}' 不存在。当前未配置任何数据库，"
                "请在管理页面 /admin 配置。")
    lines = [f"别名或项目 '{alias}' 不存在。可用配置："]
    for a in sorted(dbs):
        projects = dbs[a].get("projects") or []
        proj_str = f"（项目: {', '.join(projects)}）" if projects else ""
        lines.append(f"- {a}{proj_str}")
    lines.append("参数 alias 可传数据库别名，或管理页面配置的项目名称（项目文件夹名）。")
    return "\n".join(lines)


@app.list_tools()
async def list_tools() -> list[Tool]:
    """
    Defines the tools available to AI agents via this MCP server.
    """
    alias_prop = {
        "alias": {
            "type": "string",
            "description": (
                "数据库别名，或管理页面 /admin 中为该库配置的项目名称（项目文件夹名）。"
                "在单个 SSE 连接内通过此参数切换不同库；省略时用连接 URL ?alias 指定的别名或默认别名。"
                "建议优先传当前项目文件夹名自动匹配对应数据库。"
            )
        }
    }
    return [
        Tool(
            name="execute_sql",
            description=(
                "Execute a SQL statement against the MySQL server. "
                "Use for SELECT, DML (INSERT/UPDATE/DELETE), SHOW, DESCRIBE, and ad-hoc queries. "
                "Supports cross-database queries using database.table notation. "
                "Single statements only — use fully qualified names instead of USE statements. "
                "Write/delete statements require user confirmation: depending on the client, either a "
                "confirmation prompt appears, or the first call returns a confirm_token — show the SQL "
                "to the user, and after explicit consent re-call with the same query plus confirm_token. "
                "DDL statements (CREATE/ALTER/RENAME/GRANT/REVOKE) additionally require the alias's "
                "allow_ddl permission; deletes require allow_delete. "
                "Use the optional alias parameter to target a different configured database within a single connection."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The SQL statement to execute. Single statements only."
                    },
                    "confirm_token": {
                        "type": "string",
                        "description": (
                            "One-time confirmation token returned by a previous write attempt. "
                            "Pass it with the SAME query after the user explicitly approved the SQL."
                        )
                    },
                    **alias_prop
                },
                "required": ["query"]
            },
            annotations=ToolAnnotations(
                title="Execute SQL",
                readOnlyHint=False,
                destructiveHint=True
            )
        ),
        Tool(
            name="get_schema_info",
            description=(
                "Get column metadata for a table or all tables in the configured database: "
                "column names, data types, nullability, default values, and comments. "
                "Call this before querying an unfamiliar table. "
                "Omit table_name to see all tables at once. "
                "Accepts bare table names (uses MYSQL_DATABASE) or database.table for cross-database lookups. "
                "Use alias to target a different configured database."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Optional: bare table name, or database.table for a cross-database lookup."
                    },
                    **alias_prop
                }
            },
            annotations=ToolAnnotations(
                title="Get Schema Info",
                readOnlyHint=True,
                destructiveHint=False
            )
        ),
        Tool(
            name="get_table_sample",
            description=(
                "Fetch a small sample of rows from a table to understand its data format and content. "
                "Use alongside get_schema_info before writing complex queries. "
                "Accepts bare table names (uses MYSQL_DATABASE) or database.table for cross-database lookups. "
                "Use alias to target a different configured database."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Table to sample. Use database.table notation for cross-database queries."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of rows to return (default 5, max 20)."
                    },
                    **alias_prop
                },
                "required": ["table_name"]
            },
            annotations=ToolAnnotations(
                title="Get Table Sample",
                readOnlyHint=True,
                destructiveHint=False
            )
        ),
        Tool(
            name="search_schema",
            description=(
                "Search table names, column names, and comments by keyword across the configured "
                "database. Much cheaper than fetching all schemas on large databases — "
                "use it to locate relevant tables before calling get_schema_info. "
                "Returns matched tables and columns (kind, location, name, remark). "
                "Use alias to target a different configured database."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Keyword to search for in table names, column names, and comments (partial match)."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of matches to return (default 20, max 50)."
                    },
                    **alias_prop
                },
                "required": ["keyword"]
            },
            annotations=ToolAnnotations(
                title="Search Schema",
                readOnlyHint=True,
                destructiveHint=False
            )
        )
    ]


_NO_CONFIG_HINT = "未配置任何数据库连接，请访问管理页面 /admin 进行配置。"


def _coerce_arguments(name: str, arguments: object) -> tuple[dict, str | None]:
    """把客户端传来的 arguments 规整为 dict。

    部分客户端会把 arguments 发成 JSON 字符串；get_table_sample 的 limit
    也常被发成字符串。SDK 的 jsonschema 预校验会把这类调用直接打回
    （Input validation error），且错误信息对用户不友好，故关闭 SDK 预校验、
    在此统一做宽松兼容：失败时返回中文提示而非抛异常。
    返回 (arguments_dict, error_text)，error_text 非空表示应直接返回给客户端。
    """
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, ValueError):
            return {}, f"参数 arguments 必须是 JSON 对象，收到字符串：{arguments[:200]}"
    if not isinstance(arguments, dict):
        return {}, f"参数 arguments 必须是 JSON 对象，收到：{type(arguments).__name__}"
    if name in ("get_table_sample", "search_schema") and "limit" in arguments:
        limit = arguments["limit"]
        if isinstance(limit, str):
            try:
                arguments = {**arguments, "limit": int(limit)}
            except ValueError:
                return {}, f"参数 limit 必须是整数，收到：{limit[:50]}"
        elif isinstance(limit, float):
            arguments = {**arguments, "limit": int(limit)}
    return arguments, ""


def _resolve_for_call(alias: str | None, arguments: dict) -> tuple[str | None, dict | None, str]:
    """解析本次调用的目标条目，返回 (alias, entry, error_text)；error_text 非空表示拒绝。"""
    eff = _effective_alias(alias, arguments)
    resolved = db_config.resolve(eff)
    if eff is not None and eff != alias and resolved is None:
        return None, None, _unknown_alias_hint(eff)
    if resolved is None:
        return None, None, _NO_CONFIG_HINT
    a, entry = resolved
    return a, entry, ""


async def call_tool_impl(name: str, arguments: dict, alias: str | None = None) -> list[TextContent]:
    """
    工具调用实现：按别名路由到 execute_sql_entry / run_query_entry（read 角色）。
    alias=None 时用默认别名（databases.json 缺省时回退环境变量）。
    同时支持在 arguments 中通过 alias 参数覆盖连接级别别名（单连接多库）。
    """
    try:
        logger.info(f"Calling tool: {name} with arguments: {arguments}")
        arguments, arg_error = _coerce_arguments(name, arguments)
        if arg_error:
            return [TextContent(type="text", text=arg_error)]
        read_audit_ctx = {"channel": "read"} if _read_audit_enabled() else None

        if name == "execute_sql":
            query = arguments.get("query")
            if not query:
                raise ValueError("Query is required")
            trimmed = str(query).strip().rstrip(";")
            if top_level_semicolon_count(trimmed) > 0:
                return [TextContent(type="text", text=(
                    "Only single statements are supported. "
                    "Instead of USE statements, use fully qualified names: database.table"
                ))]
            a, entry, err = _resolve_for_call(alias, arguments)
            if err:
                return [TextContent(type="text", text=err)]
            confirm_token = arguments.get("confirm_token") or None
            return await execute_sql_entry(a, entry, query, confirm_token=confirm_token)

        elif name == "get_schema_info":
            a, entry, err = _resolve_for_call(alias, arguments)
            if err:
                return [TextContent(type="text", text=err)]
            table_name = arguments.get("table_name")
            if table_name:
                db, tbl = parse_table_arg(table_name)
                query = db_drivers.schema_sql(tbl, db, entry)
            else:
                query = db_drivers.schema_sql(None, None, entry)
            if db_drivers.entry_db_type(entry) == db_drivers.DAMENG:
                return await run_query_entry(
                    entry, query, "read",
                    row_mapper=lambda r: db_drivers.schema_row_to_csv(r, entry),
                    alias=a, tool="get_schema_info", sql_type="SCHEMA",
                    audit_ctx=read_audit_ctx,
                )
            return await run_query_entry(entry, query, "read",
                                         alias=a, tool="get_schema_info", sql_type="SCHEMA",
                                         audit_ctx=read_audit_ctx)

        elif name == "get_table_sample":
            a, entry, err = _resolve_for_call(alias, arguments)
            if err:
                return [TextContent(type="text", text=err)]
            db, tbl = parse_table_arg(arguments.get("table_name"))
            limit = min(arguments.get("limit", 5), 20)
            query = db_drivers.sample_sql(db, tbl, limit, entry)
            return await run_query_entry(entry, query, "read",
                                         alias=a, tool="get_table_sample", sql_type="SELECT",
                                         audit_ctx=read_audit_ctx)

        elif name == "search_schema":
            a, entry, err = _resolve_for_call(alias, arguments)
            if err:
                return [TextContent(type="text", text=err)]
            keyword = str(arguments.get("keyword") or "").strip()
            if not keyword:
                return [TextContent(type="text", text=(
                    "参数 keyword 必填：提供要搜索的表名/列名/注释关键字（支持部分匹配）。"))]
            try:
                limit = int(arguments.get("limit", 20))
            except (TypeError, ValueError):
                limit = 20
            limit = max(1, min(limit, 50))
            query = db_drivers.search_schema_sql(keyword, limit, entry)
            return await run_query_entry(entry, query, "read",
                                         alias=a, tool="search_schema", sql_type="SEARCH",
                                         audit_ctx=read_audit_ctx)

        else:
            raise ValueError(f"Unknown tool: {name}")
    except Exception as e:
        logger.error(f"Error in call_tool: {str(e)}")
        logger.error(traceback.format_exc())
        # Return the error as a TextContent so the client can display it.
        # This addresses Issue #50 where errors were not being reported clearly.
        return [TextContent(type="text", text=f"Error calling tool {name}: {str(e)}")]


@app.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """
    Dispatches tool calls from AI agents to the appropriate implementation logic.

    validate_input=False：SDK 的 jsonschema 预校验对非标准客户端过于严格
    （如 arguments 发成 JSON 字符串、limit 发成字符串会被直接打回），
    参数兼容由 call_tool_impl 开头的 _coerce_arguments 统一处理。
    """
    return await call_tool_impl(name, arguments, alias=None)

@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name="explore_database",
            description=(
                "Systematically explore the database: discover available tables, "
                "inspect their schemas, sample the data, and summarize what's there."
            ),
            arguments=[]
        ),
        Prompt(
            name="analyze_table",
            description=(
                "Deep-dive into a specific table: retrieve its schema, sample its data, "
                "and suggest useful queries."
            ),
            arguments=[
                PromptArgument(
                    name="table_name",
                    description="Table to analyze. Use database.table notation for cross-database queries.",
                    required=True
                )
            ]
        ),
    ]

@app.get_prompt()
async def get_prompt(name: str, arguments: dict | None) -> GetPromptResult:
    if name == "explore_database":
        return GetPromptResult(
            description="Systematic database exploration workflow",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(type="text", text=(
                        "Explore this MySQL database systematically:\n\n"
                        "1. Call list_resources to discover available tables "
                        "(or databases when MYSQL_DATABASE is not set).\n"
                        "2. Call get_schema_info with no table_name to see all table structures at once, "
                        "or for each table of interest individually.\n"
                        "3. Call get_table_sample on 2–3 representative tables to understand "
                        "data format and content.\n"
                        "4. Summarize: describe what each table stores, note relationships "
                        "(foreign keys, shared ID columns), and suggest 3–5 queries "
                        "an analyst would find useful."
                    ))
                )
            ]
        )
    elif name == "analyze_table":
        table_name = (arguments or {}).get("table_name", "")
        return GetPromptResult(
            description=f"Analysis workflow for: {table_name}",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(type="text", text=(
                        f"Analyze the table `{table_name}`:\n\n"
                        f"1. Call get_schema_info with table_name=\"{table_name}\" "
                        "to retrieve column names, types, nullability, and comments.\n"
                        f"2. Call get_table_sample with table_name=\"{table_name}\" "
                        "to see representative rows.\n"
                        "3. Based on the schema and sample, provide:\n"
                        "   - A plain-English description of what this table stores\n"
                        "   - Notable columns (primary keys, foreign keys, important fields)\n"
                        "   - Data quality observations (NULLs, patterns, value ranges)\n"
                        "   - 3–5 example SQL queries useful for analysis"
                    ))
                )
            ]
        )
    else:
        raise ValueError(f"Unknown prompt: {name}")

def max_rows_limit() -> int:
    """单次查询返回的最大行数：MYSQL_MCP_MAX_ROWS，默认 500；0 表示不限。"""
    env = os.getenv("MYSQL_MCP_MAX_ROWS")
    try:
        limit = int(env) if env is not None else 500
    except ValueError:
        limit = 500
    return max(0, limit)


_TRUNCATED_NOTE = (
    "\n⚠️ 结果已截断：仅返回前 {limit} 行。"
    "如需完整结果请加更精确的 WHERE 条件或 LIMIT 子句缩小范围。"
)


def _fetch_bounded(cursor, limit: int):
    """取结果集：limit>0 时多取一行以检测截断。返回 (rows, truncated)。"""
    if limit <= 0:
        return cursor.fetchall(), False
    rows = cursor.fetchmany(limit + 1)
    if len(rows) > limit:
        return rows[:limit], True
    return rows, False


def _format_query_result(cursor, conn, query: str, config: dict, row_mapper=None) -> tuple[list[TextContent], int | None]:
    """按查询类型格式化结果，返回 (内容列表, 行数元数据)。

    行数元数据：结果集语句为返回行数（截断时为上限值）；写语句为受影响行数。
    """
    query_upper = query.strip().upper()
    limit = max_rows_limit()

    if query_upper.startswith("SHOW TABLES"):
        tables, truncated = _fetch_bounded(cursor, limit)
        db_name = config.get("database", "all databases")
        result = [f"Tables_in_{db_name}"]
        result.extend([table[0] for table in tables])
        if truncated:
            result.append(_TRUNCATED_NOTE.format(limit=limit))
        return [TextContent(type="text", text="\n".join(result))], len(tables)

    if any(query_upper.startswith(p) for p in ["DESCRIBE ", "DESC ", "SHOW COLUMNS FROM ", "SHOW FIELDS FROM "]):
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        results = [",".join(columns)]
        for row in rows:
            results.append(",".join(str(v) if v is not None else "NULL" for v in row))
        return [TextContent(type="text", text="\n".join(results))], len(rows)

    if cursor.description is not None:
        columns = [desc[0] for desc in cursor.description]
        rows, truncated = _fetch_bounded(cursor, limit)
        if not rows:
            return [TextContent(type="text", text="Query executed successfully. No results returned.")], 0
        if row_mapper is not None:
            rows = [row_mapper(r) for r in rows]
        result = [",".join("" if v is None else str(v) for v in row) for row in rows]
        text = "\n".join([",".join(columns)] + result)
        if truncated:
            text += _TRUNCATED_NOTE.format(limit=limit)
        return [TextContent(type="text", text=text)], len(rows)

    conn.commit()
    return [TextContent(type="text", text=f"Query executed successfully. Rows affected: {cursor.rowcount}")], cursor.rowcount


async def run_query_entry(entry: dict, query: str, role: str, row_mapper=None,
                          alias: str | None = None, tool: str | None = None,
                          sql_type: str | None = None,
                          audit_ctx: dict | None = None) -> list[TextContent]:
    """用别名条目 + 指定角色账号执行 SQL；数据库报错时入库（UPDATE/DELETE 无权限除外）。

    audit_ctx（可选）传 {"channel": ...}：执行结束（成功或失败）自动记录审计，
    状态与耗时/行数均真实；不传则不记录（读操作默认不记）。
    """
    if sql_type is None:
        kw = first_keyword(query)
        sql_type = cte_main_keyword(query) if kw == "WITH" else kw
        sql_type = sql_type or classify(query).upper()
    log_alias = alias or "-"
    log_tool = tool or "-"

    def _sync_run():
        started = time.monotonic()
        with maybe_ssh_tunnel_for(entry) as (host, port):
            try:
                with conn_pool.get_connection(entry, role, host, port) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(query)
                        contents, rows = _format_query_result(
                            cursor, conn, query, db_drivers.build_config(entry, role, host, port),
                            row_mapper=row_mapper)
                if audit_ctx is not None:
                    audit.record(log_alias, sql_type, audit_ctx.get("channel", "-"), query,
                                 status="executed",
                                 elapsed_ms=int((time.monotonic() - started) * 1000),
                                 rows=rows)
                return contents
            except Exception as e:
                error_msg = db_drivers.error_message(entry, e)
                logger.error(f"Error executing SQL: {error_msg}")
                error_log.record(log_alias, log_tool, query, error_msg,
                                 sql_type=sql_type, role=role)
                if audit_ctx is not None:
                    audit.record(log_alias, sql_type, audit_ctx.get("channel", "-"), query,
                                 status="error",
                                 elapsed_ms=int((time.monotonic() - started) * 1000),
                                 rows=None)
                return [TextContent(type="text", text=f"Error executing query: {error_msg}")]
    return await anyio.to_thread.run_sync(_sync_run)

async def main():
    """
    Main entry point for the MCP server.
    Supports both STDIO (default) and SSE (HTTP) transport modes.
    """
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    if transport == "sse":
        await _run_sse_server()
    else:
        await _run_stdio_server()

async def _run_stdio_server():
    """Runs the server using standard input/output streams."""
    from mcp.server.stdio import stdio_server
    logger.info("Starting MySQL MCP server (STDIO)...")
    async with stdio_server() as (read_stream, write_stream):
        try:
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options()
            )
        except Exception as e:
            logger.error(f"Server error: {str(e)}", exc_info=True)
            raise


async def health_check(request):
    """Simple health check endpoint."""
    return Response("MySQL MCP Server is running", media_type="text/plain")


def build_starlette_app(security_settings=None):
    """构建主 HTTP app：SSE/Streamable HTTP 别名路由 + admin 挂载（可测试，无参调用即默认安全设置）。"""
    _transport_registry: dict[str, SseServerTransport] = {}
    # Streamable HTTP：每别名一个会话管理器（stateless + JSON 响应，兼容不持会话的客户端）
    _http_managers: dict[str, dict] = {}

    def _make_transport(alias: str) -> SseServerTransport:
        if alias not in _transport_registry:
            endpoint = f"/messages/{alias}/"
            _transport_registry[alias] = SseServerTransport(endpoint, security_settings=security_settings)
        return _transport_registry[alias]

    async def _ensure_http_manager(alias: str, server: Server):
        """惰性创建并启动该别名的 StreamableHTTPSessionManager（run() 须长驻后台）。"""
        cached = _http_managers.get(alias)
        if cached is not None:
            return cached["manager"]
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        manager = StreamableHTTPSessionManager(
            app=server,
            json_response=True,
            stateless=True,
            security_settings=security_settings,
        )
        started = asyncio.Event()
        exc_holder: list[BaseException] = []

        async def _run():
            try:
                async with manager.run():
                    started.set()
                    await asyncio.Event().wait()  # 常驻：manager 生命周期即进程生命周期
            except BaseException as e:  # 启动失败必须唤醒等待者，否则请求永久挂起
                exc_holder.append(e)
                started.set()

        task = asyncio.create_task(_run())
        await started.wait()
        if exc_holder:
            _http_managers.pop(alias, None)
            raise exc_holder[0]
        _http_managers[alias] = {"manager": manager, "task": task}
        return manager

    async def handle_sse(request):
        requested = request.query_params.get("alias")
        resolved = db_config.resolve(requested)
        if resolved is None:
            return PlainTextResponse(
                f"Unknown alias '{requested or ''}'. 请先在管理页面 /admin 配置数据库。", status_code=404)
        alias, _entry = resolved
        transport = _make_transport(alias)
        if alias not in _server_registry:
            _server_registry[alias] = create_alias_server(alias)
        server = _server_registry[alias]
        async with transport.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())
        return Response()

    class McpEndpoint:
        """Streamable HTTP 入口（纯 ASGI，响应由 manager 发出，handler 不再回 Response）。

        stateless + JSON 响应模式只支持 POST；GET 会被 SDK 当作 SSE 事件流挂住，
        DELETE（会话终止）对 stateless 无意义，两者直接 405。
        """

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http" and scope.get("method") != "POST":
                resp = PlainTextResponse(
                    "Method Not Allowed: stateless /mcp only accepts POST",
                    status_code=405)
                await resp(scope, receive, send)
                return
            qs = urllib.parse.parse_qs(scope.get("query_string", b"").decode())
            requested = (qs.get("alias") or [None])[0]
            resolved = db_config.resolve(requested)
            if resolved is None:
                resp = PlainTextResponse(
                    f"Unknown alias '{requested or ''}'. 请先在管理页面 /admin 配置数据库。", status_code=404)
                await resp(scope, receive, send)
                return
            alias, _entry = resolved
            if alias not in _server_registry:
                _server_registry[alias] = create_alias_server(alias)
            server = _server_registry[alias]
            manager = await _ensure_http_manager(alias, server)
            await manager.handle_request(scope, receive, send)

    class AliasMessagesEndpoint:
        async def __call__(self, scope, receive, send):
            alias = scope.get("path_params", {}).get("alias")
            transport = _transport_registry.get(alias)
            if transport is None:
                resp = PlainTextResponse("Unknown alias", status_code=404)
                await resp(scope, receive, send)
                return
            await transport.handle_post_message(scope, receive, send)

    admin_api.register_on_change(invalidate_alias)

    return Starlette(routes=[
        Route("/", endpoint=health_check),
        Route("/sse", endpoint=handle_sse),
        Route("/messages/{alias}/", endpoint=AliasMessagesEndpoint()),
        Route("/messages/{alias}", endpoint=AliasMessagesEndpoint()),
        Route("/mcp", endpoint=McpEndpoint(), methods=["GET", "POST", "DELETE"]),
        Mount("/admin", app=admin_api.create_admin_app(), name="admin"),
    ])


def _build_allowed_hosts(host: str, port: int) -> list[str]:
    """构建 SSE DNS rebinding 防护的 Host 白名单。

    - 环境变量 MCP_SSE_ALLOWED_HOSTS 显式指定时优先
    - 默认：localhost/127.0.0.1；绑定具体地址时加该地址
    - 绑定 0.0.0.0/:: 时自动探测本机全部 IPv4 加入（局域网直连可用）
    """
    allowed_hosts_env = os.getenv("MCP_SSE_ALLOWED_HOSTS", "")
    if allowed_hosts_env:
        return [h.strip() for h in allowed_hosts_env.split(",") if h.strip()]
    hosts = [f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"]
    if host in ("0.0.0.0", "::"):
        try:
            infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            for info in infos:
                ip = info[4][0]
                entry = f"{ip}:{port}"
                if entry not in hosts:
                    hosts.append(entry)
        except OSError:
            pass
    elif host not in ("127.0.0.1", "localhost"):
        hosts.append(f"{host}:{port}")
    return hosts


async def _run_sse_server():
    """
    Runs the server using Server-Sent Events (SSE) over HTTP.
    Requires 'starlette' and 'uvicorn' dependencies.
    """
    try:
        import uvicorn
    except ImportError:
        logger.error("SSE transport requires additional dependencies. Install with: pip install mysql_mcp_server[sse]")
        raise

    logger.info("Starting MySQL MCP server (SSE)...")

    host = os.getenv("MCP_SSE_HOST", "0.0.0.0")
    port_str = os.getenv("MCP_SSE_PORT") or os.getenv("PORT") or "8000"
    port = int(port_str)

    # Build security settings with DNS rebinding protection.
    # allowed_hosts controls which Host header values are accepted; without this
    # a DNS rebinding attack can relay requests through the victim's browser.
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        allowed_hosts = _build_allowed_hosts(host, port)

        logger.info(
            "SSE DNS rebinding protection enabled. Allowed hosts: %s. "
            "Override with MCP_SSE_ALLOWED_HOSTS (comma-separated).",
            ", ".join(allowed_hosts),
        )
        security_settings = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
        )
    except ImportError:
        logger.warning(
            "mcp.server.transport_security not available (upgrade mcp>=1.9.0 for DNS rebinding protection). "
            "Running without Origin/Host validation."
        )
        security_settings = None

    starlette_app = build_starlette_app(security_settings=security_settings)

    # Configure and start the Uvicorn server.
    server_config = uvicorn.Config(starlette_app, host=host, port=port, log_level="info")
    server = uvicorn.Server(server_config)
    await server.serve()

if __name__ == "__main__":
    # Start the asyncio event loop.
    asyncio.run(main())
