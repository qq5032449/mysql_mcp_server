"""项目名称字段：normalize 归一化、resolve 按项目名回退匹配、MCP 调用端到端路由。"""

import pytest
from unittest.mock import AsyncMock, patch

from mcp.types import TextContent

from mysql_mcp_server import db_config
from mysql_mcp_server.server import call_tool_impl


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


class TestNormalizeProjects:
    def test_none_gives_empty_list(self):
        assert db_config.normalize_entry(make_entry())["projects"] == []

    def test_string_comma_split(self):
        e = db_config.normalize_entry(make_entry(projects="my-web, order-service"))
        assert e["projects"] == ["my-web", "order-service"]

    def test_string_chinese_comma_and_spaces(self):
        e = db_config.normalize_entry(make_entry(projects="a，b  c"))
        assert e["projects"] == ["a", "b", "c"]

    def test_list_input(self):
        e = db_config.normalize_entry(make_entry(projects=["p1", "", "p2"]))
        assert e["projects"] == ["p1", "p2"]

    def test_invalid_type_rejected(self):
        with pytest.raises(ValueError):
            db_config.normalize_entry(make_entry(projects=123))


class TestResolveByProject:
    @pytest.fixture(autouse=True)
    def _cfg(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db_config, "CONFIG_FILE", tmp_path / "databases.json")
        monkeypatch.setattr(db_config, "CONFIG_DIR", tmp_path)
        for k in ("MYSQL_USER", "MYSQL_PASSWORD"):
            monkeypatch.delenv(k, raising=False)
        db_config.reset_cache()
        yield
        db_config.reset_cache()

    def _save_two(self):
        db_config.save_config({
            "default_alias": "db1",
            "databases": {
                "db1": make_entry(database="db1"),
                "db2": make_entry(database="db2", projects=["webapp", "api"]),
            },
        })

    def test_exact_alias_still_wins(self):
        self._save_two()
        a, entry = db_config.resolve("db2")
        assert a == "db2"
        assert entry["database"] == "db2"

    def test_project_name_fallback(self):
        self._save_two()
        a, entry = db_config.resolve("webapp")
        assert a == "db2"
        assert entry["database"] == "db2"

    def test_unknown_returns_none(self):
        self._save_two()
        assert db_config.resolve("nope") is None

    def test_multiple_hits_takes_smallest_alias(self):
        db_config.save_config({
            "default_alias": None,
            "databases": {
                "zeta": make_entry(projects=["dup"]),
                "alpha": make_entry(projects=["dup"]),
            },
        })
        a, _ = db_config.resolve("dup")
        assert a == "alpha"

    def test_deep_copy_entry(self):
        self._save_two()
        _, entry = db_config.resolve("webapp")
        entry["projects"].append("mutated")
        _, again = db_config.resolve("webapp")
        assert "mutated" not in again["projects"]

    def test_default_alias_without_arg(self):
        self._save_two()
        a, _ = db_config.resolve(None)
        assert a == "db1"


class TestMcpRoutingByProject:
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
        return await call_tool_impl(name, args, alias)

    @pytest.mark.asyncio
    async def test_execute_sql_routes_by_project_name(self):
        db_config.save_config({
            "default_alias": "db1",
            "databases": {
                "db1": make_entry(),
                "proj-db": make_entry(projects=["my_project_folder"]),
            },
        })
        ok = TextContent(type="text", text="ok")
        with patch("mysql_mcp_server.server.execute_sql_entry", new=AsyncMock(return_value=[ok])) as ex:
            r = await self._call(
                "execute_sql",
                {"query": "SELECT 1", "alias": "my_project_folder"},
                alias=None,
            )
        assert r[0].text == "ok"
        resolved_alias, _, query = ex.await_args.args
        assert resolved_alias == "proj-db"
        assert query == "SELECT 1"

    @pytest.mark.asyncio
    async def test_unknown_alias_lists_projects_in_hint(self):
        db_config.save_config({
            "default_alias": "db1",
            "databases": {"db1": make_entry(projects=["known-app"])},
        })
        r = await self._call("execute_sql", {"query": "SELECT 1", "alias": "wrong"}, alias=None)
        text = r[0].text
        assert "wrong" in text
        assert "known-app" in text          # 提示中展示已配置的项目名
        assert "项目名称" in text             # 告知可传项目名

    @pytest.mark.asyncio
    async def test_connection_alias_project_match(self):
        """连接级 ?alias= 也支持传项目名。"""
        db_config.save_config({
            "default_alias": None,
            "databases": {"svc": make_entry(projects=["folder-x"])},
        })
        ok = TextContent(type="text", text="ok")
        with patch("mysql_mcp_server.server.execute_sql_entry", new=AsyncMock(return_value=[ok])) as ex:
            await self._call("execute_sql", {"query": "SELECT 1"}, alias="folder-x")
        assert ex.await_args.args[0] == "svc"

    @pytest.mark.asyncio
    async def test_get_table_sample_routes_by_project(self):
        db_config.save_config({
            "default_alias": None,
            "databases": {"samp": make_entry(projects=["sample-app"])},
        })
        ok = TextContent(type="text", text="ok")
        with patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])):
            r = await self._call(
                "get_table_sample",
                {"table_name": "t", "alias": "sample-app"},
                alias=None,
            )
        assert r[0].text == "ok"
