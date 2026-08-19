import pytest
from unittest.mock import AsyncMock, patch

from mcp.types import TextContent

from mysql_mcp_server import db_config
from mysql_mcp_server.server import build_connector_config, execute_sql_entry


def make_entry(**kw):
    base = {
        "host": "localhost", "port": 3306, "database": "mydb", "charset": "utf8mb4",
        "sql_mode": "TRADITIONAL", "connect_timeout": 10,
        "read_user": {"user": "reader", "password": "rp"},
        "write_user": {"user": "writer", "password": "wp"},
        "write_policy": "client_confirm", "allow_delete": False,
    }
    base.update(kw)
    return base


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


@pytest.mark.asyncio
async def test_token_confirm_executes():
    """携带有效令牌 → 用 write 账号执行，令牌消费。"""
    from mysql_mcp_server import server
    server._pending_tokens.clear()
    ok = TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])) as rq:
        first = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1")
        token = first[0].text.split("confirm_token=")[1].split()[0].strip("`'\"")
        second = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1", confirm_token=token)
    assert text_of(second) == "done"
    assert rq.await_args.args[2] == "write"
    assert len(server._pending_tokens) == 0  # 已消费


@pytest.mark.asyncio
async def test_token_replay_rejected():
    from mysql_mcp_server import server
    server._pending_tokens.clear()
    ok = TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])):
        first = await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1")
        token = first[0].text.split("confirm_token=")[1].split()[0].strip("`'\"")
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
    from mysql_mcp_server import audit as audit_mod
    monkeypatch.setattr(audit_mod, "LOG_DIR", tmp_path)
    audit_mod.clear()
    ok = TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value="accept")), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])):
        await execute_sql_entry("db1", make_entry(allow_delete=True),
                                "WITH c AS (SELECT 1) DELETE FROM t")
    assert audit_mod.list_entries()[-1]["type"] == "DELETE"  # 不应是 WITH


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
