import asyncio
import logging
import os
import sys
import re
import socket
import time
import subprocess
import traceback
from contextlib import contextmanager
from typing import List, Optional, Tuple, Any

import anyio
from mysql.connector import connect, Error
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

from mysql_mcp_server import admin_api, audit, db_config
from mysql_mcp_server.sql_classify import classify, first_keyword, cte_main_keyword

# Load environment variables from .env file if it exists.
# This allows for easy local configuration of database and SSH credentials.
load_dotenv()

# Configure logging to provide visibility into server operations.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mysql_mcp_server")

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

def parse_table_arg(name: str) -> Tuple[Optional[str], str]:
    """Split an optional 'database.table' argument into (db, table) parts, validating each."""
    if "." in name:
        db, tbl = name.split(".", 1)
        return validate_identifier(db), validate_identifier(tbl)
    return None, validate_identifier(name)

# ---------------------------------------------------------------------------
# 别名/双账号支持：从 databases.json 条目构造连接配置并执行
# ---------------------------------------------------------------------------

def build_connector_config(entry: dict, role: str, host=None, port=None) -> dict:
    """从别名条目构造 mysql.connector 配置；role='read'|'write' 选择对应账号。"""
    acct = entry[f"{role}_user"]
    config = {
        "host": host or entry["host"],
        "port": port or int(entry.get("port", 3306)),
        "user": acct["user"],
        "password": acct["password"],
        "charset": entry.get("charset") or "utf8mb4",
        "collation": entry.get("collation") or "utf8mb4_unicode_ci",
        "autocommit": True,
        "sql_mode": entry.get("sql_mode") or "TRADITIONAL",
        "connect_timeout": int(entry.get("connect_timeout", 10)),
    }
    if entry.get("database"):
        config["database"] = entry["database"]
    return {k: v for k, v in config.items() if v is not None}


@contextmanager
def maybe_ssh_tunnel_for(entry: dict):
    """按别名条目的 ssh 配置建立隧道；未启用时直连。"""
    ssh = entry.get("ssh") or {}
    if not ssh.get("enable"):
        yield entry["host"], int(entry.get("port", 3306))
        return

    local_port = int(ssh.get("local_port", 3330))
    logger.info(
        f"Starting SSH tunnel for alias: {ssh.get('user')}@{ssh.get('host')}:{ssh.get('port', 22)} "
        f"-> {local_port}:{ssh.get('remote_host', 'localhost')}:{ssh.get('remote_port', 3306)}"
    )
    ssh_cmd = [
        'ssh', '-i', ssh.get("key_path") or '',
        '-N', '-o', 'ExitOnForwardFailure=yes', '-o', 'BatchMode=yes',
        '-L', f"{local_port}:{ssh.get('remote_host', 'localhost')}:{ssh.get('remote_port', 3306)}",
        f"{ssh.get('user')}@{ssh.get('host')}", '-p', str(ssh.get("port", 22)),
    ]
    ssh_proc = subprocess.Popen(ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(2)
        if ssh_proc.poll() is not None:
            stderr = ssh_proc.stderr.read().decode()
            raise RuntimeError(f"SSH tunnel process exited prematurely: {stderr}")
        yield "127.0.0.1", local_port
    finally:
        try:
            ssh_proc.terminate()
            ssh_proc.wait(timeout=5)
        except Exception as e:
            logger.error(f"Error terminating SSH tunnel: {e}")


async def elicit_confirm(alias: str, kind_label: str, query: str, timeout: float = 30.0) -> str | None:
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
                message=f"确认在数据库别名 [{alias}] 上执行{kind_label}操作：\n\n{query}",
                requestedSchema={"type": "object", "properties": {}},
            )
            return result.action
    except Exception as e:
        logger.warning(f"Elicitation failed: {e}")
        return None
    return None  # 超时


_KIND_LABELS = {"write": "写", "delete": "删除类"}

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
    """三级判定 + 双账号执行 + 确认（elicitation 弹窗或聊天内令牌二次确认）。"""
    kind = classify(query)
    kw = first_keyword(query)
    if kw == "WITH":
        kw = cte_main_keyword(query)
    sql_type = kw or kind.upper()

    if kind == "read":
        return await run_query_entry(entry, query, "read")

    if kind == "delete" and not entry.get("allow_delete"):
        audit.record(alias, sql_type, "-", query, status="rejected_delete_disabled")
        return [TextContent(type="text", text="该别名未开启删除权限，请在管理页面开启后重试。")]

    # skip_confirm 开启：写操作（不含删除类）免二次确认直接执行
    if kind == "write" and entry.get("skip_confirm"):
        result = await run_query_entry(entry, query, "write")
        audit.record(alias, sql_type, "skip_confirm", query)
        return result

    # 携带令牌：校验通过则直接用操作账号执行（聊天内二次确认的第二步）
    if confirm_token:
        verdict = await _consume_token(confirm_token, alias, query)
        if verdict == "ok":
            result = await run_query_entry(entry, query, "write")
            audit.record(alias, sql_type, "token", query)
            return result
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
    action = await elicit_confirm(alias, label, query)

    if action == "accept":
        result = await run_query_entry(entry, query, "write")
        audit.record(alias, sql_type, "elicitation", query)
        return result
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
        f"令牌 5 分钟内有效、一次性且仅对本条 SQL 有效。\n\n{query}"))]

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
    _, entry = resolved

    def _sync_list():
        with maybe_ssh_tunnel_for(entry) as (host, port):
            config = build_connector_config(entry, "read", host, port)
            try:
                with connect(**config) as conn:
                    with conn.cursor() as cursor:
                        if "database" not in config:
                            # Multi-database mode: list available databases.
                            cursor.execute("SHOW DATABASES")
                            databases = cursor.fetchall()
                            return [
                                Resource(
                                    uri=f"mysql://database/{db[0]}",
                                    name=f"database_{db[0]}",
                                    mimeType="text/plain",
                                    description=f"MySQL database: {db[0]}"
                                )
                                for db in databases if db[0] not in SYSTEM_DATABASES
                            ]
                        else:
                            # Single-database mode: list tables in the configured database.
                            cursor.execute("SHOW TABLES")
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
            except Error as e:
                error_msg = getattr(e, 'msg', None) or str(e) or 'Unknown MySQL error'
                logger.error(f"Failed to list resources: {error_msg}")
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
    _, entry = resolved

    def _sync_read():
        with maybe_ssh_tunnel_for(entry) as (host, port):
            config = build_connector_config(entry, "read", host, port)
            uri_str = str(uri)
            if not uri_str.startswith("mysql://"):
                raise ValueError(f"Invalid URI scheme: {uri_str}")

            parts = uri_str[8:].split('/')

            # Handle requests to list tables in a specific database.
            if len(parts) >= 2 and parts[0] == "database":
                db_name = validate_identifier(parts[1])
                try:
                    with connect(**config) as conn:
                        with conn.cursor() as cursor:
                            cursor.execute(f"USE `{db_name}`")
                            cursor.execute("SHOW TABLES")
                            tables = cursor.fetchall()
                            result = [f"Tables in database '{db_name}':"]
                            result.extend([table[0] for table in tables])
                            return "\n".join(result)
                except Error as e:
                    error_msg = getattr(e, 'msg', None) or str(e) or 'Unknown MySQL error'
                    raise RuntimeError(f"Database error: {error_msg}")

            # Handle requests to read data from a specific table.
            table = validate_identifier(parts[0])
            try:
                with connect(**config) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(f"SELECT * FROM `{table}` LIMIT 100")
                        columns = [desc[0] for desc in cursor.description]
                        rows = cursor.fetchall()
                        # Format output as simple CSV-like text.
                        result = [",".join("" if v is None else str(v) for v in row) for row in rows]
                        return "\n".join([",".join(columns)] + result)
            except Error as e:
                error_msg = getattr(e, 'msg', None) or str(e) or 'Unknown MySQL error'
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
    s.call_tool()(_call_tool)
    return s


def _effective_alias(connection_alias: str | None, arguments: dict) -> str | None:
    p = arguments.get("alias")
    if isinstance(p, str) and p.strip():
        return p.strip()
    return connection_alias


def _unknown_alias_hint(alias: str) -> str:
    cfg = db_config.load_config()
    available = sorted(cfg.get("databases", {}).keys())
    avail_str = ", ".join(available) if available else "无"
    return f"别名 '{alias}' 不存在。可用别名: {avail_str}。请在管理页面 /admin 配置或使用 list_aliases 查看。"


@app.list_tools()
async def list_tools() -> list[Tool]:
    """
    Defines the tools available to AI agents via this MCP server.
    """
    alias_prop = {
        "alias": {
            "type": "string",
            "description": "数据库别名（对应管理页面 /admin 中配置的别名）。在一个 SSE 连接内通过此参数切换不同库；省略则使用连接 URL 中 ?alias 指定的别名或默认别名。"
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
        )
    ]


_NO_CONFIG_HINT = "未配置任何数据库连接，请访问管理页面 /admin 进行配置。"


async def call_tool_impl(name: str, arguments: dict, alias: str | None = None) -> list[TextContent]:
    """
    工具调用实现：按别名路由到 execute_sql_entry / run_query_entry（read 角色）。
    alias=None 时用默认别名（databases.json 缺省时回退环境变量）。
    同时支持在 arguments 中通过 alias 参数覆盖连接级别别名（单连接多库）。
    """
    try:
        logger.info(f"Calling tool: {name} with arguments: {arguments}")

        if name == "execute_sql":
            query = arguments.get("query")
            if not query:
                raise ValueError("Query is required")
            if ";" in query.strip().rstrip(";"):
                return [TextContent(type="text", text=(
                    "Only single statements are supported. "
                    "Instead of USE statements, use fully qualified names: database.table"
                ))]
            eff = _effective_alias(alias, arguments)
            resolved = db_config.resolve(eff)
            if eff is not None and eff != alias and resolved is None:
                return [TextContent(type="text", text=_unknown_alias_hint(eff))]
            if resolved is None:
                return [TextContent(type="text", text=_NO_CONFIG_HINT)]
            a, entry = resolved
            confirm_token = arguments.get("confirm_token") or None
            return await execute_sql_entry(a, entry, query, confirm_token=confirm_token)

        elif name == "get_schema_info":
            eff = _effective_alias(alias, arguments)
            resolved = db_config.resolve(eff)
            if eff is not None and eff != alias and resolved is None:
                return [TextContent(type="text", text=_unknown_alias_hint(eff))]
            if resolved is None:
                return [TextContent(type="text", text=_NO_CONFIG_HINT)]
            _, entry = resolved
            table_name = arguments.get("table_name")
            if table_name:
                db, tbl = parse_table_arg(table_name)
                schema_filter = f"TABLE_SCHEMA = '{db}'" if db else "TABLE_SCHEMA = DATABASE()"
                query = f"SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT FROM information_schema.COLUMNS WHERE {schema_filter} AND TABLE_NAME = '{tbl}' ORDER BY ORDINAL_POSITION"
            else:
                query = "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME, ORDINAL_POSITION"
            return await run_query_entry(entry, query, "read")

        elif name == "get_table_sample":
            eff = _effective_alias(alias, arguments)
            resolved = db_config.resolve(eff)
            if eff is not None and eff != alias and resolved is None:
                return [TextContent(type="text", text=_unknown_alias_hint(eff))]
            if resolved is None:
                return [TextContent(type="text", text=_NO_CONFIG_HINT)]
            _, entry = resolved
            db, tbl = parse_table_arg(arguments.get("table_name"))
            limit = min(arguments.get("limit", 5), 20)
            table_ref = f"`{db}`.`{tbl}`" if db else f"`{tbl}`"
            query = f"SELECT * FROM {table_ref} LIMIT {limit}"
            return await run_query_entry(entry, query, "read")

        else:
            raise ValueError(f"Unknown tool: {name}")
    except Exception as e:
        logger.error(f"Error in call_tool: {str(e)}")
        logger.error(traceback.format_exc())
        # Return the error as a TextContent so the client can display it.
        # This addresses Issue #50 where errors were not being reported clearly.
        return [TextContent(type="text", text=f"Error calling tool {name}: {str(e)}")]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """
    Dispatches tool calls from AI agents to the appropriate implementation logic.
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

def _format_query_result(cursor, conn, query: str, config: dict) -> list[TextContent]:
    """按查询类型格式化结果（从原 run_query 提取，逻辑不变）。"""
    query_upper = query.strip().upper()

    if query_upper.startswith("SHOW TABLES"):
        tables = cursor.fetchall()
        db_name = config.get("database", "all databases")
        result = [f"Tables_in_{db_name}"]
        result.extend([table[0] for table in tables])
        return [TextContent(type="text", text="\n".join(result))]

    if any(query_upper.startswith(p) for p in ["DESCRIBE ", "DESC ", "SHOW COLUMNS FROM ", "SHOW FIELDS FROM "]):
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        results = [",".join(columns)]
        for row in rows:
            results.append(",".join(str(v) if v is not None else "NULL" for v in row))
        return [TextContent(type="text", text="\n".join(results))]

    if cursor.description is not None:
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        if not rows:
            return [TextContent(type="text", text="Query executed successfully. No results returned.")]
        result = [",".join("" if v is None else str(v) for v in row) for row in rows]
        return [TextContent(type="text", text="\n".join([",".join(columns)] + result))]

    conn.commit()
    return [TextContent(type="text", text=f"Query executed successfully. Rows affected: {cursor.rowcount}")]


async def run_query_entry(entry: dict, query: str, role: str) -> list[TextContent]:
    """用别名条目 + 指定角色账号执行 SQL。"""
    def _sync_run():
        with maybe_ssh_tunnel_for(entry) as (host, port):
            config = build_connector_config(entry, role, host, port)
            try:
                with connect(**config) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(query)
                        return _format_query_result(cursor, conn, query, config)
            except Error as e:
                error_msg = getattr(e, 'msg', None) or str(e) or 'Unknown MySQL error'
                logger.error(f"Error executing SQL: {error_msg}")
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
    """构建主 HTTP app：SSE 别名路由 + admin 挂载（可测试，无参调用即默认安全设置）。"""
    _transport_registry: dict[str, SseServerTransport] = {}

    def _make_transport(alias: str) -> SseServerTransport:
        if alias not in _transport_registry:
            endpoint = f"/messages/{alias}/"
            _transport_registry[alias] = SseServerTransport(endpoint, security_settings=security_settings)
        return _transport_registry[alias]

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
