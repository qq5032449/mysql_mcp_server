import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from mcp.types import TextContent

from mysql_mcp_server import db_config
from mysql_mcp_server.server import build_connector_config, execute_sql_entry


def make_entry(**kw):
    base = {
        "host": "localhost", "port": 3306, "database": "mydb", "charset": "utf8mb4",
        "sql_mode": "TRADITIONAL", "connect_timeout": 10,
        "read_user": {"user": "reader", "password": "rp"},
        "write_user": {"user": "writer", "password": "wp"},
        "write_policy": "client_confirm", "allow_delete": False, "skip_confirm": False,
    }
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_write_skip_confirm_executes_directly():
    """skip_confirm 开启：写操作免二次确认，直接用 write 账号执行并审计。"""
    ok = TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock()) as ec, \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])) as rq:
        result = await execute_sql_entry("db1", make_entry(skip_confirm=True), "UPDATE t SET x=1")
    assert text_of(result) == "done"
    assert rq.await_args.args[2] == "write"
    ec.assert_not_awaited()  # 完全跳过确认通道


@pytest.mark.asyncio
async def test_delete_not_affected_by_skip_confirm():
    """skip_confirm 不放宽删除门槛：DELETE 仍受 allow_delete 控制。"""
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock()) as ec:
        result = await execute_sql_entry("db1", make_entry(skip_confirm=True, allow_delete=False), "DELETE FROM t")
    assert "未开启删除权限" in text_of(result)
    ec.assert_not_awaited()


@pytest.mark.asyncio
async def test_ddl_allowed_with_confirm(tmp_path, monkeypatch):
    """DDL 权限开启时：ALTER/CREATE 走正常确认后执行。"""
    ok = TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value="accept")), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])) as rq:
        r1 = await execute_sql_entry("db1", make_entry(allow_ddl=True), "ALTER TABLE t ADD COLUMN c INT")
        r2 = await execute_sql_entry("db1", make_entry(allow_ddl=True), "CREATE TABLE t2 (id INT)")
    assert text_of(r1) == "done"
    assert text_of(r2) == "done"
    assert rq.await_args_list[0].args[2] == "write"


@pytest.mark.asyncio
async def test_ddl_blocked_without_allow_ddl():
    """allow_ddl 默认关闭：CREATE/ALTER 等 DDL 被拒绝（行为变化：不再仅靠确认放行）。"""
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock()) as ec:
        result = await execute_sql_entry("db1", make_entry(), "ALTER TABLE t ADD COLUMN c INT")
    assert "DDL" in text_of(result)
    ec.assert_not_awaited()


class TestBuildConnectorConfig:
    def test_read_role(self):
        c = build_connector_config(make_entry(), "read")
        assert c["user"] == "reader"
        assert c["password"] == "rp"
        assert c["database"] == "mydb"
        assert c["autocommit"] is True

    def test_write_role(self):
        c = build_connector_config(make_entry(), "write")
        assert c["user"] == "writer"
        assert c["password"] == "wp"

    def test_no_database_key_when_multi(self):
        c = build_connector_config(make_entry(database=None), "read")
        assert "database" not in c

    def test_host_port_override(self):
        c = build_connector_config(make_entry(), "read", host="127.0.0.1", port=3330)
        assert c["host"] == "127.0.0.1"
        assert c["port"] == 3330


def text_of(result):
    return result[0].text


@pytest.mark.asyncio
async def test_read_executes_with_read_account():
    with patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[TextContent(type="text", text="ok")])) as rq:
        result = await execute_sql_entry("db1", make_entry(), "SELECT 1")
        assert text_of(result) == "ok"
        rq.assert_awaited_once()
        entry, query, role = rq.await_args.args
        assert query == "SELECT 1"
        assert role == "read"


@pytest.mark.asyncio
async def test_delete_blocked_without_permission():
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock()) as ec:
        result = await execute_sql_entry("db1", make_entry(allow_delete=False), "DELETE FROM t")
    assert "未开启删除权限" in text_of(result)
    ec.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_elicitation_accept():
    ok = TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value="accept")) as ec, \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])) as rq:
        result = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1")
    assert text_of(result) == "done"
    role = rq.await_args.args[2]
    assert role == "write"


@pytest.mark.asyncio
async def test_write_elicitation_decline():
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value="decline")), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock()) as rq:
        result = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1")
    assert "拒绝" in text_of(result)
    rq.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_fallback_issues_token():
    """无 elicitation 且 client_confirm：不执行，签发一次性令牌要求二次确认。"""
    from mysql_mcp_server import server
    server._pending_tokens.clear()
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock()) as rq:
        result = await execute_sql_entry("db1", make_entry(write_policy="client_confirm"), "INSERT INTO t VALUES (1)")
    text = text_of(result)
    assert "confirm_token" in text
    assert "INSERT INTO t VALUES (1)" in text
    rq.assert_not_awaited()
    assert len(server._pending_tokens) == 1


def _get_token(result) -> str:
    return result[0].text.split("confirm_token=")[1].split()[0].strip("`'\"")


def _simulate_user_confirmation(server, token: str) -> None:
    """模拟用户确认耗时（跳过 3 秒冷静期）。"""
    server._pending_tokens[token]["not_before"] = 0.0


@pytest.mark.asyncio
async def test_token_confirm_executes():
    """携带有效令牌（经用户确认、超过冷静期）→ 用 write 账号执行，令牌消费。"""
    from mysql_mcp_server import server
    server._pending_tokens.clear()
    ok = TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])) as rq:
        first = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1")
        token = _get_token(first)
        _simulate_user_confirmation(server, token)
        second = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1", confirm_token=token)
    assert text_of(second) == "done"
    assert rq.await_args.args[2] == "write"
    assert len(server._pending_tokens) == 0  # 已消费


@pytest.mark.asyncio
async def test_token_used_too_early_rejected_and_invalidated():
    """签发后立即使用（6 秒冷静期内）→ 拒绝执行且令牌作废，提示必须走用户确认。"""
    from mysql_mcp_server import server
    server._pending_tokens.clear()
    ok = TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])) as rq:
        first = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1")
        token = _get_token(first)
        # 客户端拿到令牌后立即回传（不可能已征求用户同意）
        early = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1", confirm_token=token)
        assert "AskUserQuestion" in text_of(early)
        rq.assert_not_awaited()
        # 令牌已作废：即使时间流逝也无法再用
        assert token not in server._pending_tokens
        revived = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1", confirm_token=token)
        assert "令牌" in text_of(revived)
        rq.assert_not_awaited()


@pytest.mark.asyncio
async def test_token_cooling_period_value():
    """冷静期为 6 秒。"""
    from mysql_mcp_server import server
    assert server._TOKEN_MIN_DELAY_SECONDS == 6.0


@pytest.mark.asyncio
async def test_token_replay_rejected():
    from mysql_mcp_server import server
    server._pending_tokens.clear()
    ok = TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])):
        first = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1")
        token = _get_token(first)
        _simulate_user_confirmation(server, token)
        await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1", confirm_token=token)
        replay = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1", confirm_token=token)
    assert "令牌" in text_of(replay)


@pytest.mark.asyncio
async def test_token_wrong_sql_rejected():
    """令牌仅对签发时的同一条 SQL 有效。"""
    from mysql_mcp_server import server
    server._pending_tokens.clear()
    ok = TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])):
        first = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1")
        token = first[0].text.split("confirm_token=")[1].split()[0].strip("`'\"")
        other = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=999", confirm_token=token)
    assert "令牌" in text_of(other)


@pytest.mark.asyncio
async def test_token_wrong_alias_rejected():
    from mysql_mcp_server import server
    server._pending_tokens.clear()
    ok = TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])):
        first = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1")
        token = first[0].text.split("confirm_token=")[1].split()[0].strip("`'\"")
        other = await execute_sql_entry("db2", make_entry(), "UPDATE t SET x=1", confirm_token=token)
    assert "令牌" in text_of(other)


@pytest.mark.asyncio
async def test_token_expired_rejected():
    from mysql_mcp_server import server
    server._pending_tokens.clear()
    ok = TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])):
        first = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1")
        token = first[0].text.split("confirm_token=")[1].split()[0].strip("`'\"")
        # 手动把令牌置为过期
        server._pending_tokens[token]["expires"] = 0.0
        expired = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1", confirm_token=token)
    assert "令牌" in text_of(expired)


@pytest.mark.asyncio
async def test_write_fallback_elicitation_only_rejected():
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock()) as rq:
        result = await execute_sql_entry("db1", make_entry(write_policy="elicitation_only"), "INSERT INTO t VALUES (1)")
    assert "elicitation_only" in text_of(result)
    rq.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_allowed_with_confirm():
    ok = TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value="accept")), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])) as rq:
        result = await execute_sql_entry("db1", make_entry(allow_delete=True), "DROP TABLE t")
    assert text_of(result) == "done"
    assert rq.await_args.args[2] == "write"


@pytest.mark.asyncio
async def test_audit_recorded_on_token_fallback(tmp_path, monkeypatch):
    """无 elicitation + client_confirm：签发令牌时审计 pending_token。"""
    from mysql_mcp_server import audit as audit_mod, server
    monkeypatch.setattr(audit_mod, "LOG_DIR", tmp_path)
    audit_mod.clear()
    server._pending_tokens.clear()
    ok = TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])):
        await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1")
    entries = audit_mod.list_entries()
    assert entries[-1]["channel"] == "token"
    assert entries[-1]["status"] == "pending_token"
    assert entries[-1]["type"] == "UPDATE"


@pytest.mark.asyncio
async def test_cancel_rejected():
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value="cancel")), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock()) as rq:
        result = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1")
    assert "拒绝" in text_of(result)
    rq.assert_not_awaited()


@pytest.mark.asyncio
async def test_cte_delete_audit_type(tmp_path, monkeypatch):
    """CTE 内含 SELECT 的 DELETE：审计 type 应记 DELETE 而非 WITH（真实执行路径）。"""
    from mysql_mcp_server import audit as audit_mod
    from mysql_mcp_server.server import conn_pool

    monkeypatch.setattr(audit_mod, "LOG_DIR", tmp_path)
    audit_mod.clear()
    cur = MagicMock()
    cur.description = None  # DELETE 无结果集
    cur.rowcount = 1
    cur.__enter__.return_value = cur
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cur
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value="accept")), \
         patch.object(conn_pool, "get_connection", return_value=conn):
        await execute_sql_entry("db1", make_entry(allow_delete=True),
                                "WITH c AS (SELECT 1) DELETE FROM t")
    entries = audit_mod.list_entries()
    assert entries and entries[-1]["type"] == "DELETE"  # 不应是 WITH
    assert entries[-1]["status"] == "executed"
    assert entries[-1]["rows"] == 1


class TestCallToolImpl:
    @pytest.fixture(autouse=True)
    def _cfg(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db_config, "CONFIG_FILE", tmp_path / "databases.json")
        monkeypatch.setattr(db_config, "CONFIG_DIR", tmp_path)
        for k in ("MYSQL_USER", "MYSQL_PASSWORD"):
            monkeypatch.delenv(k, raising=False)
        db_config.reset_cache()
        yield
        db_config.reset_cache()

    async def _call(self, name, args, alias=None):
        from mysql_mcp_server.server import call_tool_impl
        return await call_tool_impl(name, args, alias)

    @pytest.mark.asyncio
    async def test_execute_sql_no_config_hint(self):
        r = await self._call("execute_sql", {"query": "SELECT 1"})
        assert "管理页面" in text_of(r)

    @pytest.mark.asyncio
    async def test_execute_sql_routes_to_alias(self):
        db_config.save_config({"default_alias": "db1", "databases": {"db1": make_entry()}})
        ok = TextContent(type="text", text="ok")
        with patch("mysql_mcp_server.server.execute_sql_entry", new=AsyncMock(return_value=[ok])) as ex:
            r = await self._call("execute_sql", {"query": "SELECT 1"}, alias="db1")
        assert text_of(r) == "ok"
        a, entry, q = ex.await_args.args
        assert a == "db1"
        assert q == "SELECT 1"

    @pytest.mark.asyncio
    async def test_execute_sql_unknown_alias_hint(self):
        db_config.save_config({"default_alias": "db1", "databases": {"db1": make_entry()}})
        r = await self._call("execute_sql", {"query": "SELECT 1"}, alias="nope")
        assert "管理页面" in text_of(r)

    @pytest.mark.asyncio
    async def test_multi_statement_rejected(self):
        db_config.save_config({"default_alias": "db1", "databases": {"db1": make_entry()}})
        r = await self._call("execute_sql", {"query": "USE x; SELECT 1"})
        assert "single statements" in text_of(r)

    @pytest.mark.asyncio
    async def test_get_table_sample_uses_read_role(self):
        db_config.save_config({"default_alias": "db1", "databases": {"db1": make_entry()}})
        ok = TextContent(type="text", text="ok")
        with patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])) as rq:
            r = await self._call("get_table_sample", {"table_name": "t"}, alias="db1")
        assert text_of(r) == "ok"
        assert rq.await_args.args[1].startswith("SELECT * FROM")
        assert rq.await_args.args[2] == "read"

    @pytest.mark.asyncio
    async def test_get_schema_info_read_role(self):
        db_config.save_config({"default_alias": "db1", "databases": {"db1": make_entry()}})
        ok = TextContent(type="text", text="ok")
        with patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])) as rq:
            await self._call("get_schema_info", {"table_name": "t"}, alias="db1")
        assert "information_schema.COLUMNS" in rq.await_args.args[1]
        assert rq.await_args.args[2] == "read"


class TestCreateAliasServer:
    def test_independent_instances(self):
        from mysql_mcp_server.server import create_alias_server
        s1 = create_alias_server("db1")
        s2 = create_alias_server("db2")
        assert s1 is not s2
        assert s1.name == "mysql_mcp_server"

    def test_registry_and_invalidate(self):
        from mysql_mcp_server import server
        server._server_registry["db1"] = object()
        server.invalidate_alias("db1")
        assert "db1" not in server._server_registry
        server._server_registry["a"] = object()
        server._server_registry["b"] = object()
        server.invalidate_alias()  # 清空全部
        assert server._server_registry == {}


class TestSseAppStructure:
    def test_build_starlette_app_routes(self):
        from mysql_mcp_server.server import build_starlette_app
        app = build_starlette_app()
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/" in paths
        assert "/sse" in paths
        assert "/messages/{alias}" in paths or "/messages/{alias}/" in paths
        assert "/admin" in paths  # Mount


class TestBuildAllowedHosts:
    def test_loopback_defaults(self):
        from mysql_mcp_server.server import _build_allowed_hosts
        hosts = _build_allowed_hosts("127.0.0.1", 8000)
        assert "localhost:8000" in hosts and "127.0.0.1:8000" in hosts

    def test_wildcard_includes_local_ipv4(self):
        from mysql_mcp_server.server import _build_allowed_hosts
        import ipaddress
        hosts = _build_allowed_hosts("0.0.0.0", 8000)
        # 全部条目必须是 "localhost:port" 或合法 IP:port（IPv6 带方括号）
        ips = []
        for h in hosts:
            ip_str = h.rsplit(":", 1)[0].strip("[]")
            if ip_str != "localhost":
                ipaddress.ip_address(ip_str)
                ips.append(ip_str)
        # 至少包含回环
        assert any(ipaddress.ip_address(i).is_loopback for i in ips)

    def test_specific_host_included(self):
        from mysql_mcp_server.server import _build_allowed_hosts
        hosts = _build_allowed_hosts("192.168.84.22", 9000)
        assert "192.168.84.22:9000" in hosts


@pytest.mark.asyncio
async def test_read_db_error_recorded_to_error_log(tmp_path, monkeypatch):
    """读 SQL 打到数据库报错 → error_log 入库（含别名/工具/SQL/数据库错误）。"""
    from mysql_mcp_server import error_log, server
    from mysql_mcp_server.db_drivers import error_message
    monkeypatch.setattr(error_log, "DB_FILE", tmp_path / "error_log.db")
    monkeypatch.setattr(error_log, "FALLBACK_FILE", tmp_path / "error_log_fallback.log")
    error_log.clear()
    boom = Exception("no such table")
    with patch("mysql_mcp_server.server.conn_pool.get_connection", side_effect=boom):
        result = await execute_sql_entry("db1", make_entry(), "SELECT * FROM missing_table")
    assert "Error executing query" in text_of(result)
    rows = error_log.query_errors()
    assert len(rows) == 1
    assert rows[0]["alias"] == "db1"
    assert rows[0]["tool"] == "execute_sql"
    assert "missing_table" in rows[0]["sql"]
    assert rows[0]["db_error"] == error_message(make_entry(), boom)


@pytest.mark.asyncio
async def test_write_db_error_recorded_with_confirm(tmp_path, monkeypatch):
    """确认通过的写 SQL 执行报错 → 同样入库。"""
    from mysql_mcp_server import error_log
    monkeypatch.setattr(error_log, "DB_FILE", tmp_path / "error_log.db")
    monkeypatch.setattr(error_log, "FALLBACK_FILE", tmp_path / "error_log_fallback.log")
    error_log.clear()
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value="accept")), \
         patch("mysql_mcp_server.server.conn_pool.get_connection",
               side_effect=Exception("Duplicate entry '1' for key 'PRIMARY'")):
        result = await execute_sql_entry("db1", make_entry(), "INSERT INTO t VALUES (1)")
    assert "Error executing query" in text_of(result)
    rows = error_log.query_errors()
    assert len(rows) == 1
    assert "Duplicate entry" in rows[0]["db_error"]


@pytest.mark.asyncio
async def test_permission_denied_not_recorded(tmp_path, monkeypatch):
    """数据库返回 UPDATE 无权限 → 不入库。"""
    from mysql_mcp_server import error_log
    monkeypatch.setattr(error_log, "DB_FILE", tmp_path / "error_log.db")
    monkeypatch.setattr(error_log, "FALLBACK_FILE", tmp_path / "error_log_fallback.log")
    error_log.clear()
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value="accept")), \
         patch("mysql_mcp_server.server.conn_pool.get_connection",
               side_effect=Exception("UPDATE command denied to user 'w'@'%' for table 't'")):
        result = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1")
    assert "command denied" in text_of(result)
    assert error_log.query_errors() == []


@pytest.mark.asyncio
async def test_coerce_arguments_json_string():
    """arguments 发成 JSON 字符串 → 正常解析执行（兼容非标准客户端）。"""
    from mysql_mcp_server.server import call_tool_impl
    from mysql_mcp_server import db_config
    import tempfile, os
    tmp = tempfile.mkdtemp()
    old_file, old_dir = db_config.CONFIG_FILE, db_config.CONFIG_DIR
    db_config.CONFIG_FILE = type(old_file)(os.path.join(tmp, "databases.json"))
    db_config.CONFIG_DIR = type(old_dir)(tmp)
    db_config.reset_cache()
    try:
        db_config.save_config({"default_alias": "db1", "databases": {"db1": make_entry()}})
        ok = TextContent(type="text", text="ok")
        with patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])):
            r = await call_tool_impl("execute_sql", {"query": "SELECT 1", "extra": "x"})
        assert text_of(r) == "ok"
    finally:
        db_config.CONFIG_FILE, db_config.CONFIG_DIR = old_file, old_dir
        db_config.reset_cache()


@pytest.mark.asyncio
async def test_coerce_limit_string_and_invalid():
    """get_table_sample 的 limit 传字符串数字 → 转 int 执行；非法字符串 → 中文提示。"""
    from mysql_mcp_server.server import _coerce_arguments
    args, err = _coerce_arguments("get_table_sample", {"table_name": "t", "limit": "5"})
    assert err == "" and args["limit"] == 5
    args, err = _coerce_arguments("get_table_sample", {"table_name": "t", "limit": "abc"})
    assert "整数" in err
    args, err = _coerce_arguments("execute_sql", '{"query": "SELECT 1"}')
    assert err == "" and args == {"query": "SELECT 1"}
    _, err = _coerce_arguments("execute_sql", "not-json")
    assert "JSON 对象" in err
    _, err = _coerce_arguments("execute_sql", ["x"])
    assert "JSON 对象" in err


@pytest.mark.asyncio
async def test_missing_table_name_friendly():
    """get_table_sample 缺 table_name → 中文提示而非内部错误。"""
    with patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock()) as rq:
        result = await execute_sql_entry("db1", make_entry(), "SELECT 1")
        assert rq.await_count == 1  # 对照：正常调用不受影响
    from mysql_mcp_server.server import call_tool_impl, db_config
    import tempfile, os
    tmp = tempfile.mkdtemp()
    old_file, old_dir = db_config.CONFIG_FILE, db_config.CONFIG_DIR
    db_config.CONFIG_FILE = type(old_file)(os.path.join(tmp, "databases.json"))
    db_config.CONFIG_DIR = type(old_dir)(tmp)
    db_config.reset_cache()
    try:
        db_config.save_config({"default_alias": "db1", "databases": {"db1": make_entry()}})
        with patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock()) as rq:
            r = await call_tool_impl("get_table_sample", {}, alias="db1")
        assert "table_name" in text_of(r)
        rq.assert_not_awaited()
    finally:
        db_config.CONFIG_FILE, db_config.CONFIG_DIR = old_file, old_dir
        db_config.reset_cache()


def test_parse_table_arg_none():
    """parse_table_arg(None) → 中文 ValueError。"""
    from mysql_mcp_server.server import parse_table_arg
    import pytest as _pytest
    with _pytest.raises(ValueError, match="table_name"):
        parse_table_arg(None)
