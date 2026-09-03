"""达梦驱动抽象层与 db_type 配置：方言 SQL 构造、连接参数、类型归一化（纯函数 + mock，无需真实达梦实例）。"""

import sys
import types

import pytest
from unittest.mock import MagicMock, patch

from mysql_mcp_server import db_config, db_drivers


def dm_entry(**kw):
    base = {
        "db_type": "dameng",
        "host": "dmhost", "port": 5236, "database": "DMHR", "charset": "utf8mb4",
        "sql_mode": "TRADITIONAL", "connect_timeout": 10,
        "read_user": {"user": "reader", "password": "rp"},
        "write_user": {"user": "writer", "password": "wp"},
        "write_policy": "client_confirm", "allow_delete": False, "skip_confirm": False,
        "projects": [],
    }
    base.update(kw)
    return base


def mysql_entry(**kw):
    base = dm_entry(db_type="mysql", port=3306, database="mydb")
    base.update(kw)
    return base


class TestNormalizeDbType:
    def test_dameng_case_insensitive(self):
        assert db_drivers.normalize_db_type("Dameng") == "dameng"
        assert db_drivers.normalize_db_type("  dameng ") == "dameng"

    def test_mysql_default_on_empty_or_unknown(self):
        assert db_drivers.normalize_db_type(None) == "mysql"
        assert db_drivers.normalize_db_type("") == "mysql"
        assert db_drivers.normalize_db_type("oracle") == "mysql"

    def test_entry_db_type_missing_key(self):
        assert db_drivers.entry_db_type({}) == "mysql"


class TestQuoteIdent:
    def test_mysql_backtick(self):
        assert db_drivers.quote_ident("t", mysql_entry()) == "`t`"
        assert db_drivers.quote_ident("a`b", mysql_entry()) == "`a``b`"

    def test_dameng_double_quote(self):
        assert db_drivers.quote_ident("T", dm_entry()) == '"T"'
        assert db_drivers.quote_ident('a"b', dm_entry()) == '"a""b"'

    def test_qualified_table(self):
        assert db_drivers.qualified_table("DMHR", "EMP", dm_entry()) == '"DMHR"."EMP"'
        assert db_drivers.qualified_table(None, "t", mysql_entry()) == "`t`"
        assert db_drivers.qualified_table("db", "t", mysql_entry()) == "`db`.`t`"


class TestDialectSql:
    def test_schema_sql_dameng_with_table(self):
        sql = db_drivers.schema_sql("EMP", None, dm_entry())
        assert "ALL_TAB_COLUMNS" in sql
        assert "UPPER(TABLE_NAME) = UPPER('EMP')" in sql
        assert "UPPER(OWNER) = UPPER('DMHR')" in sql  # 默认模式取条目 database

    def test_schema_sql_dameng_explicit_db_wins(self):
        sql = db_drivers.schema_sql("EMP", "OTHER", dm_entry())
        assert "UPPER(OWNER) = UPPER('OTHER')" in sql

    def test_schema_sql_dameng_no_table(self):
        sql = db_drivers.schema_sql(None, None, dm_entry())
        assert sql.startswith("SELECT TABLE_NAME, COLUMN_NAME")
        assert "ALL_TAB_COLUMNS" in sql

    def test_schema_sql_mysql_unchanged(self):
        sql = db_drivers.schema_sql("t", "mydb", mysql_entry())
        assert "information_schema.COLUMNS" in sql
        assert "TABLE_SCHEMA = 'mydb'" in sql

    def test_schema_sql_injection_escaped(self):
        sql = db_drivers.schema_sql("x'y", None, dm_entry())
        assert "x''y" in sql

    def test_sample_sql(self):
        assert db_drivers.sample_sql("DMHR", "EMP", 5, dm_entry()) == 'SELECT * FROM "DMHR"."EMP" LIMIT 5'
        assert db_drivers.sample_sql(None, "t", 5, mysql_entry()) == "SELECT * FROM `t` LIMIT 5"

    def test_list_tables_sql(self):
        assert "USER_TABLES" in db_drivers.list_tables_sql(dm_entry())
        assert db_drivers.list_tables_sql(mysql_entry()) == "SHOW TABLES"

    def test_list_schemas_sql_dameng_filters_system(self):
        sql = db_drivers.list_schemas_sql(dm_entry())
        assert "ALL_USERS" in sql
        assert "'SYS'" in sql and "'CTISYS'" in sql

    def test_schema_row_nullable_mapping(self):
        row = ("C1", "VARCHAR", "Y", None, "注释")
        assert db_drivers.schema_row_to_csv(row, dm_entry())[2] == "YES"
        row = ("C1", "VARCHAR", "N", None, None)
        assert db_drivers.schema_row_to_csv(row, dm_entry())[2] == "NO"
        row = ("c", "int", "YES", None, None)
        assert db_drivers.schema_row_to_csv(row, mysql_entry()) == row


class TestBuildConfig:
    def test_dameng_params(self):
        c = db_drivers.build_config(dm_entry(), "read")
        assert c["user"] == "reader"
        assert c["password"] == "rp"
        assert c["server"] == "dmhost"
        assert c["port"] == 5236
        assert c["schema"] == "DMHR"
        assert c["autoCommit"] is True
        assert c["login_timeout"] == 10000
        assert "host" not in c and "database" not in c and "charset" not in c

    def test_dameng_no_schema_when_multi(self):
        c = db_drivers.build_config(dm_entry(database=None), "read")
        assert "schema" not in c

    def test_dameng_host_port_override(self):
        c = db_drivers.build_config(dm_entry(), "write", host="127.0.0.1", port=5333)
        assert c["server"] == "127.0.0.1" and c["port"] == 5333
        assert c["user"] == "writer"

    def test_mysql_params_unchanged(self):
        c = db_drivers.build_config(mysql_entry(), "read")
        assert c["host"] == "dmhost" and c["user"] == "reader"
        assert c["database"] == "mydb" and c["autocommit"] is True


class TestConnectEntry:
    def _fake_dm_module(self):
        mod = types.ModuleType("dmPython")
        mod.Error = type("DmError", (Exception,), {})
        mod.connect = MagicMock()
        return mod

    def test_dameng_uses_dmPython(self):
        fake = self._fake_dm_module()
        with patch.dict(sys.modules, {"dmPython": fake}):
            db_drivers.connect_entry(dm_entry(), "read")
        kwargs = fake.connect.call_args.kwargs
        assert kwargs["server"] == "dmhost" and kwargs["port"] == 5236

    def test_dameng_missing_driver_hint(self):
        with patch.dict(sys.modules, {"dmPython": None}):
            with pytest.raises(RuntimeError, match="dmPython"):
                db_drivers.connect_entry(dm_entry(), "read")

    def test_connect_error_class(self):
        fake = self._fake_dm_module()
        with patch.dict(sys.modules, {"dmPython": fake}):
            assert db_drivers.connect_error_class(dm_entry()) is fake.Error
        from mysql.connector import Error as MySQLError
        assert db_drivers.connect_error_class(mysql_entry()) is MySQLError


class TestDbConfigDbType:
    @pytest.fixture(autouse=True)
    def _cfg(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db_config, "CONFIG_FILE", tmp_path / "databases.json")
        monkeypatch.setattr(db_config, "CONFIG_DIR", tmp_path)
        for k in ("MYSQL_USER", "MYSQL_PASSWORD"):
            monkeypatch.delenv(k, raising=False)
        db_config.reset_cache()
        yield
        db_config.reset_cache()

    def _base(self, **kw):
        b = {
            "host": "h", "port": 5236,
            "read_user": {"user": "r", "password": "p"},
            "write_user": {"user": "w", "password": "p"},
        }
        b.update(kw)
        return b

    def test_validate_ok_both_types(self):
        db_config.validate_entry("a", self._base(db_type="mysql"))
        db_config.validate_entry("b", self._base(db_type="dameng"))
        db_config.validate_entry("c", self._base())  # 缺省合法

    def test_validate_rejects_unknown(self):
        with pytest.raises(ValueError, match="db_type"):
            db_config.validate_entry("a", self._base(db_type="oracle"))

    def test_normalize_defaults_mysql(self):
        out = db_config.normalize_entry(self._base())
        assert out["db_type"] == "mysql"
        out = db_config.normalize_entry(self._base(db_type="DAMENG"))
        assert out["db_type"] == "dameng"

    def test_env_entry_is_mysql(self, monkeypatch):
        monkeypatch.setenv("MYSQL_USER", "u")
        monkeypatch.setenv("MYSQL_PASSWORD", "p")
        assert db_config.load_config()["databases"]["default"]["db_type"] == "mysql"

    def test_old_config_without_db_type_resolves(self):
        db_config.save_config({"default_alias": "old", "databases": {"old": self._base()}})
        a, e = db_config.resolve("old")
        assert a == "old"
        assert db_drivers.entry_db_type(e) == "mysql"


class TestDamengEndToEnd:
    """mock 连接层，验证达梦条目端到端发出方言正确的 SQL。"""

    @pytest.fixture(autouse=True)
    def _cfg(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db_config, "CONFIG_FILE", tmp_path / "databases.json")
        monkeypatch.setattr(db_config, "CONFIG_DIR", tmp_path)
        for k in ("MYSQL_USER", "MYSQL_PASSWORD"):
            monkeypatch.delenv(k, raising=False)
        db_config.reset_cache()
        db_config.save_config({
            "default_alias": "dm1",
            "databases": {
                "dm1": dm_entry(database="DMHR", projects=["dmapp"]),
                "my1": mysql_entry(database="mydb"),
            },
        })
        yield
        db_config.reset_cache()

    def _fake_conn(self, rows=(), columns=("C",)):
        cur = MagicMock()
        cur.description = [(c,) for c in columns]
        cur.fetchall.return_value = list(rows)
        cur.__enter__.return_value = cur
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value = cur
        return conn, cur

    @pytest.mark.asyncio
    async def test_get_schema_info_uses_dm_dictionary(self):
        from mcp.types import TextContent
        from mysql_mcp_server.server import call_tool_impl
        conn, cur = self._fake_conn(
            rows=[("EMPNO", "INTEGER", "N", None, None)],
            columns=("COLUMN_NAME", "DATA_TYPE", "NULLABLE", "DATA_DEFAULT", "COMMENTS"),
        )
        with patch("mysql_mcp_server.db_drivers.connect_entry", return_value=conn) as ce:
            r = await call_tool_impl("get_schema_info", {"table_name": "EMP", "alias": "dm1"})
        sent = cur.execute.call_args.args[0]
        assert "ALL_TAB_COLUMNS" in sent
        assert '"DMHR"' not in sent  # 模式走 OWNER 过滤而非引用拼接
        assert "UPPER(OWNER) = UPPER('DMHR')" in sent
        assert ce.call_args.args[1] == "read"
        assert "EMPNO" in r[0].text and ",NO" in r[0].text

    @pytest.mark.asyncio
    async def test_get_table_sample_double_quote(self):
        from mysql_mcp_server.server import call_tool_impl
        conn, cur = self._fake_conn(rows=[(1,)], columns=("ID",))
        with patch("mysql_mcp_server.db_drivers.connect_entry", return_value=conn):
            await call_tool_impl("get_table_sample", {"table_name": "DMHR.EMP", "alias": "dm1"})
        sent = cur.execute.call_args.args[0]
        assert sent == 'SELECT * FROM "DMHR"."EMP" LIMIT 5'

    @pytest.mark.asyncio
    async def test_project_name_routes_to_dameng(self):
        from mcp.types import TextContent
        from mysql_mcp_server.server import call_tool_impl, execute_sql_entry
        from unittest.mock import AsyncMock
        ok = TextContent(type="text", text="ok")
        with patch("mysql_mcp_server.server.execute_sql_entry", new=AsyncMock(return_value=[ok])) as ex:
            r = await call_tool_impl("execute_sql", {"query": "SELECT 1", "alias": "dmapp"})
        assert r[0].text == "ok"
        assert ex.await_args.args[0] == "dm1"  # 项目名命中达梦别名

    @pytest.mark.asyncio
    async def test_mysql_and_dameng_coexist(self):
        """同一进程内 MySQL 与达梦按条目类型各自走方言。"""
        from mysql_mcp_server.server import call_tool_impl
        conn_dm, cur_dm = self._fake_conn(rows=[("X",)], columns=("TABLE_NAME",))
        conn_my, cur_my = self._fake_conn(rows=[("t",)], columns=("TABLE_NAME",))
        calls = {"dm": None, "my": None}

        def _fake_connect(entry, role, host=None, port=None):
            if db_drivers.entry_db_type(entry) == "dameng":
                calls["dm"] = entry
                return conn_dm
            calls["my"] = entry
            return conn_my

        with patch("mysql_mcp_server.db_drivers.connect_entry", side_effect=_fake_connect):
            await call_tool_impl("get_table_sample", {"table_name": "EMP", "alias": "dm1"})
            await call_tool_impl("get_table_sample", {"table_name": "t", "alias": "my1"})
        assert cur_dm.execute.call_args.args[0] == 'SELECT * FROM "EMP" LIMIT 5'
        assert cur_my.execute.call_args.args[0] == "SELECT * FROM `t` LIMIT 5"
        assert calls["dm"] is not None and calls["my"] is not None
