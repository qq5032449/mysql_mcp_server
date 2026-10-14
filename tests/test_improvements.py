"""0.5.0 新增能力：行数上限、多语句判定、WHERE 警告、allow_ddl、search_schema、
query_timeout、连接池开关、错误日志保留期、读审计开关。"""

import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import TextContent

from mysql_mcp_server import db_config, error_log
from mysql_mcp_server.server import call_tool_impl, execute_sql_entry
from mysql_mcp_server.server import conn_pool
from mysql_mcp_server.sql_classify import (
    DDL_PREFIXES,
    has_top_level_where,
    top_level_semicolon_count,
)
from mysql_mcp_server.db_drivers import (
    build_config,
    like_pattern,
    query_timeout_seconds,
    search_schema_sql,
)


def text_of(result):
    return result[0].text


def make_entry(**kw):
    base = {
        "host": "localhost", "port": 3306, "database": "mydb", "charset": "utf8mb4",
        "sql_mode": "TRADITIONAL", "connect_timeout": 10,
        "read_user": {"user": "reader", "password": "rp"},
        "write_user": {"user": "writer", "password": "wp"},
        "write_policy": "client_confirm", "allow_delete": False, "allow_ddl": False,
        "skip_confirm": False,
    }
    base.update(kw)
    return base


def fake_conn(rows=(), columns=("C",)):
    cur = MagicMock()
    cur.description = [(c,) for c in columns]
    cur.fetchall.return_value = list(rows)
    cur.fetchmany.return_value = list(rows)
    cur.__enter__.return_value = cur
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cur
    return conn, cur


# ---------------------------------------------------------------------------
# sql_classify：顶层分号 / WHERE 检测 / DDL 集合
# ---------------------------------------------------------------------------

class TestTopLevelSemicolon:
    def test_single_statement_with_semicolon_in_string(self):
        assert top_level_semicolon_count("INSERT INTO t VALUES ('a;b')") == 0

    def test_two_statements(self):
        assert top_level_semicolon_count("SELECT 1; SELECT 2") == 1

    def test_trailing_semicolons_stripped_by_caller(self):
        assert top_level_semicolon_count("SELECT 1") == 0

    def test_semicolon_in_comment_ignored(self):
        assert top_level_semicolon_count("SELECT 1 -- ; comment\n, 2") == 0

    def test_semicolon_in_subquery_not_top_level(self):
        # 子查询（括号内）的分号不在顶层：不算多语句（顶层才是语句边界）
        assert top_level_semicolon_count("SELECT (SELECT 1; )") == 0


class TestHasTopLevelWhere:
    def test_update_with_where(self):
        assert has_top_level_where("UPDATE t SET x=1 WHERE id=2") is True

    def test_update_without_where(self):
        assert has_top_level_where("UPDATE t SET x=1") is False

    def test_subquery_where_not_top_level(self):
        assert has_top_level_where("UPDATE t SET x=(SELECT id FROM b WHERE b.id=1)") is False

    def test_where_in_string_literal_ignored(self):
        assert has_top_level_where("UPDATE t SET x='WHERE'") is False

    def test_delete_with_where(self):
        assert has_top_level_where("DELETE FROM t WHERE 1=1") is True


def test_ddl_prefixes_content():
    assert "CREATE" in DDL_PREFIXES and "ALTER" in DDL_PREFIXES
    assert "DROP" not in DDL_PREFIXES and "TRUNCATE" not in DDL_PREFIXES  # 归 allow_delete 管


# ---------------------------------------------------------------------------
# execute_sql：WHERE 警告 + allow_ddl + 多语句 + 行数上限
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_where_warning_in_token_message():
    """UPDATE 无 WHERE：令牌确认文案带全表警告；有 WHERE 则无。"""
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)):
        r = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1")
    assert "WHERE 条件" in text_of(r)
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)):
        r2 = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1 WHERE id=1")
    assert "WHERE 条件" not in text_of(r2)


@pytest.mark.asyncio
async def test_semicolon_in_string_not_multi_statement():
    """VALUES 里的分号不应被误判为多语句。"""
    conn, cur = fake_conn(rows=[(1,)], columns=("N",))
    with patch.object(conn_pool, "get_connection", return_value=conn):
        r = await call_tool_impl("execute_sql", {"query": "SELECT 'a;b' AS c"})
    assert "Only single statements" not in text_of(r)


@pytest.mark.asyncio
async def test_result_row_limit_and_truncation_note(monkeypatch):
    """SELECT 结果超过 MYSQL_MCP_MAX_ROWS 截断并附提示。"""
    monkeypatch.setenv("MYSQL_MCP_MAX_ROWS", "10")
    rows = [(i,) for i in range(50)]
    conn, cur = fake_conn(rows=rows, columns=("N",))
    # fetchmany(11) 应只取 11 行
    cur.fetchmany.side_effect = lambda n: rows[:n]
    with patch.object(conn_pool, "get_connection", return_value=conn):
        r = await call_tool_impl("execute_sql", {"query": "SELECT N FROM big"})
    lines = text_of(r).split("\n")
    assert len([ln for ln in lines if ln.isdigit()]) == 10
    assert "结果已截断" in text_of(r)


@pytest.mark.asyncio
async def test_result_row_limit_disabled(monkeypatch):
    monkeypatch.setenv("MYSQL_MCP_MAX_ROWS", "0")
    rows = [(i,) for i in range(30)]
    conn, cur = fake_conn(rows=rows, columns=("N",))
    with patch.object(conn_pool, "get_connection", return_value=conn):
        r = await call_tool_impl("execute_sql", {"query": "SELECT N FROM big"})
    assert "结果已截断" not in text_of(r)
    assert "29" in text_of(r)


# ---------------------------------------------------------------------------
# search_schema
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg_db1(tmp_path, monkeypatch):
    monkeypatch.setattr(db_config, "CONFIG_FILE", tmp_path / "databases.json")
    monkeypatch.setattr(db_config, "CONFIG_DIR", tmp_path)
    monkeypatch.delenv("MYSQL_USER", raising=False)
    monkeypatch.delenv("MYSQL_PASSWORD", raising=False)
    db_config.reset_cache()
    db_config.save_config({"default_alias": "db1", "databases": {"db1": make_entry()}})
    yield
    db_config.reset_cache()


@pytest.mark.asyncio
async def test_search_schema_builds_and_runs(cfg_db1):
    conn, cur = fake_conn(rows=[("TABLE", "mydb.users", "users", "")], columns=("KIND", "LOC", "NAME", "REMARK"))
    with patch.object(conn_pool, "get_connection", return_value=conn):
        r = await call_tool_impl("search_schema", {"keyword": "user"})
    sent = cur.execute.call_args.args[0]
    assert "information_schema.TABLES" in sent
    assert "information_schema.COLUMNS" in sent
    assert "TABLE_SCHEMA = 'mydb'" in sent
    assert "\\%user\\%" in sent  # LIKE 模式转义 % 通配符
    assert "users" in text_of(r)


@pytest.mark.asyncio
async def test_search_schema_multi_db_scope(cfg_db1):
    cfg = db_config.load_config()
    cfg["databases"]["db1"]["database"] = None
    db_config.save_config(cfg)
    conn, cur = fake_conn()
    with patch.object(conn_pool, "get_connection", return_value=conn):
        await call_tool_impl("search_schema", {"keyword": "x"})
    sent = cur.execute.call_args.args[0]
    assert "NOT IN ('information_schema'" in sent


@pytest.mark.asyncio
async def test_search_schema_requires_keyword(cfg_db1):
    r = await call_tool_impl("search_schema", {"keyword": "  "})
    assert "keyword 必填" in text_of(r)


@pytest.mark.asyncio
async def test_search_schema_limit_coerced_and_capped(cfg_db1):
    conn, cur = fake_conn()
    with patch.object(conn_pool, "get_connection", return_value=conn):
        await call_tool_impl("search_schema", {"keyword": "x", "limit": "99"})
    sent = cur.execute.call_args.args[0]
    assert sent.endswith("LIMIT 50")


def test_like_pattern_escapes_wildcards():
    assert like_pattern("50%_o\\ff") == "'50\\%\\_o\\\\ff'"


def test_search_schema_sql_dameng():
    e = make_entry(db_type="dameng", database="DMHR")
    sql = search_schema_sql("emp", 20, e)
    assert "ALL_TABLES" in sql and "ALL_TAB_COLUMNS C" in sql
    assert "UPPER(OWNER) = UPPER('DMHR')" in sql
    # 列注释改由 ALL_COL_COMMENTS 提供（老版本达梦数据字典无 COMMENTS 列）
    assert "LEFT JOIN ALL_COL_COMMENTS M" in sql and "M.COMMENTS" in sql
    assert "ESCAPE" in sql


# ---------------------------------------------------------------------------
# query_timeout
# ---------------------------------------------------------------------------

def test_build_config_read_timeout_default(monkeypatch):
    monkeypatch.delenv("MYSQL_MCP_QUERY_TIMEOUT", raising=False)
    cfg = build_config(make_entry(), "read")
    assert cfg["read_timeout"] == 60 and cfg["write_timeout"] == 60


def test_build_config_timeout_entry_overrides_env(monkeypatch):
    monkeypatch.setenv("MYSQL_MCP_QUERY_TIMEOUT", "5")
    cfg = build_config(make_entry(query_timeout=30), "read")
    assert cfg["read_timeout"] == 30


def test_build_config_timeout_zero_disables(monkeypatch):
    cfg = build_config(make_entry(query_timeout=0), "read")
    assert "read_timeout" not in cfg


def test_query_timeout_seconds_invalid_falls_back(monkeypatch):
    monkeypatch.delenv("MYSQL_MCP_QUERY_TIMEOUT", raising=False)
    assert query_timeout_seconds({}) == 60
    assert query_timeout_seconds({"query_timeout": "bad"}) == 60


# ---------------------------------------------------------------------------
# conn_pool 开关与降级
# ---------------------------------------------------------------------------

def test_pool_size_for_resolution(monkeypatch):
    monkeypatch.delenv("MYSQL_MCP_POOL_SIZE", raising=False)
    assert conn_pool.pool_size_for({}) == 3
    assert conn_pool.pool_size_for({"pool_size": 0}) == 0
    monkeypatch.setenv("MYSQL_MCP_POOL_SIZE", "8")
    assert conn_pool.pool_size_for({}) == 8
    assert conn_pool.pool_size_for({"pool_size": 1}) == 1


def test_pool_disabled_goes_direct(monkeypatch):
    """pool_size=0：直连（走 db_drivers.connect_entry）。"""
    calls = {}

    def _fake_connect(entry, role, host=None, port=None):
        calls["role"] = role
        return "CONN"

    with patch("mysql_mcp_server.db_drivers.connect_entry", side_effect=_fake_connect):
        got = conn_pool.get_connection(make_entry(pool_size=0), "read")
    assert got == "CONN" and calls["role"] == "read"
    conn_pool.clear_all()


def test_pool_fingerprint_differs_by_password():
    k1 = conn_pool._fingerprint(make_entry(), "read", None, None)
    k2 = conn_pool._fingerprint(make_entry(read_user={"user": "reader", "password": "other"}), "read", None, None)
    assert k1 != k2
    assert len(k1) == 16  # 短哈希，不含明文配置


# ---------------------------------------------------------------------------
# error_log 保留期
# ---------------------------------------------------------------------------

def test_cleanup_old_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(error_log, "DB_FILE", tmp_path / "error_log.db")
    monkeypatch.setattr(error_log, "FALLBACK_FILE", tmp_path / "fb.log")
    conn = sqlite3.connect(str(tmp_path / "error_log.db"))
    error_log._ensure_schema(conn)
    old = "2020-01-01 00:00:00"
    conn.execute(
        f"INSERT INTO {error_log.TABLE_NAME} (time, alias, tool, sql_type, role, sql, db_error)"
        " VALUES (?, 'a', 't', 'S', 'read', 'q', 'e')", (old,))
    conn.commit()
    removed = error_log.cleanup_old(days=30, conn=conn)
    assert removed == 1
    left = conn.execute(f"SELECT COUNT(*) FROM {error_log.TABLE_NAME}").fetchone()[0]
    conn.close()
    assert left == 0


def test_cleanup_zero_days_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(error_log, "DB_FILE", tmp_path / "error_log.db")
    conn = sqlite3.connect(str(tmp_path / "error_log.db"))
    error_log._ensure_schema(conn)
    assert error_log.cleanup_old(days=0, conn=conn) == 0
    conn.close()


# ---------------------------------------------------------------------------
# 读审计开关
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_audit_disabled_by_default(monkeypatch, cfg_db1):
    from mysql_mcp_server import audit
    monkeypatch.delenv("MYSQL_MCP_AUDIT_READ", raising=False)
    audit.clear()
    conn, cur = fake_conn(rows=[(1,)], columns=("N",))
    with patch.object(conn_pool, "get_connection", return_value=conn):
        await call_tool_impl("execute_sql", {"query": "SELECT 1"})
    assert audit.list_entries() == []


@pytest.mark.asyncio
async def test_read_audit_enabled_records(monkeypatch, cfg_db1):
    from mysql_mcp_server import audit
    monkeypatch.setenv("MYSQL_MCP_AUDIT_READ", "true")
    audit.clear()
    conn, cur = fake_conn(rows=[(1,)], columns=("N",))
    with patch.object(conn_pool, "get_connection", return_value=conn):
        await call_tool_impl("execute_sql", {"query": "SELECT 1"})
    entries = audit.list_entries()
    assert len(entries) == 1
    assert entries[0]["channel"] == "read"
    assert entries[0]["rows"] == 1
    assert entries[0]["elapsed_ms"] is not None
