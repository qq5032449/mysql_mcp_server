"""错误提示增强与保存校验：Unknown column 附真实列、语法错误附引用符提示、
达梦数据字典缺 COMMENTS 时降级重试、保存数据库时自动校验库存在。"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from starlette.testclient import TestClient

from mysql_mcp_server import admin_api, audit, db_config, sql_hints
from mysql_mcp_server.server import call_tool_impl, conn_pool


def text_of(result):
    return result[0].text


def make_entry(**kw):
    base = {
        "host": "localhost", "port": 3306, "database": "mydb", "charset": "utf8mb4",
        "sql_mode": "TRADITIONAL", "connect_timeout": 10,
        "read_user": {"user": "reader", "password": "rp"},
        "write_user": {"user": "writer", "password": "wp"},
        "write_policy": "client_confirm", "allow_delete": False,
        "allow_ddl": False, "skip_confirm": False,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------

class TestClassification:
    def test_unknown_column_mysql(self):
        assert sql_hints.unknown_column_name(
            "1054 (42S22): Unknown column 'tenant_id' in 'field list'") == "tenant_id"

    def test_unknown_column_dameng(self):
        assert sql_hints.unknown_column_name(
            "[CODE:-2111]Error in line: 1\nInvalid column name [DEL_FLAG]") == "DEL_FLAG"

    def test_not_a_column_error(self):
        assert sql_hints.unknown_column_name("Table 'vein.x' doesn't exist") is None
        assert sql_hints.unknown_column_name("") is None

    def test_syntax_error_detected(self):
        assert sql_hints.is_syntax_error(
            "1064 (42000): You have an error in your SQL syntax; check the manual ...")
        assert not sql_hints.is_syntax_error("Unknown column 'a' in 'field list'")

    def test_missing_comments_detected(self):
        assert sql_hints.is_missing_comments_error(
            "Invalid column name [COMMENTS]")
        assert sql_hints.is_missing_comments_error(
            "1054 (42S22): Unknown column 'COMMENTS' in 'field list'")
        assert not sql_hints.is_missing_comments_error("Unknown column 'tenant_id' in 'field list'")


class TestExtractTables:
    def test_from_with_db(self):
        assert sql_hints.extract_tables(
            "SELECT * FROM medical-my.consultation WHERE id=1") == [("medical-my", "consultation")]

    def test_backticked_and_quoted(self):
        assert sql_hints.extract_tables(
            "SELECT * FROM `rpms-chs`.`sys_user`") == [("rpms-chs", "sys_user")]
        assert sql_hints.extract_tables('SELECT * FROM "DMHR"."EMP"') == [("DMHR", "EMP")]

    def test_join_update_into(self):
        got = sql_hints.extract_tables(
            "SELECT * FROM a JOIN b ON a.id=b.id")
        assert got == [(None, "a"), (None, "b")]
        assert sql_hints.extract_tables("UPDATE t SET x=1 WHERE id=2") == [(None, "t")]
        assert sql_hints.extract_tables("INSERT INTO t (a) VALUES (1)") == [(None, "t")]

    def test_dedup_and_limit(self):
        got = sql_hints.extract_tables("SELECT * FROM t JOIN t ON 1=1 JOIN u ON 1=1")
        assert got == [(None, "t"), (None, "u")]

    def test_system_schema_skipped(self):
        assert sql_hints.extract_tables(
            "SELECT * FROM information_schema.COLUMNS") == []

    def test_subquery_not_matched(self):
        assert sql_hints.extract_tables("SELECT (SELECT 1) AS x FROM DUAL") == []


# ---------------------------------------------------------------------------
# augment_error：附真实列清单 / 引用符提示
# ---------------------------------------------------------------------------

class TestAugmentError:
    def test_no_hint_for_unrelated_error(self):
        assert sql_hints.augment_error(make_entry(), "SELECT 1", "Table 'x' doesn't exist") \
            == "Table 'x' doesn't exist"

    def test_column_hint_appends_real_columns(self):
        cur = MagicMock()
        cur.fetchall.return_value = [("id",), ("user_name",), ("status",)]
        cur.__enter__.return_value = cur
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value = cur
        with patch.object(conn_pool, "get_connection", return_value=conn):
            out = sql_hints.augment_error(
                make_entry(), "SELECT tenant_id FROM sys_user",
                "1054 (42S22): Unknown column 'tenant_id' in 'field list'")
        assert "Unknown column 'tenant_id'" in out
        assert "sys_user 的实际列" in out
        assert "id, user_name, status" in out
        sent = cur.execute.call_args.args[0]
        assert "information_schema.COLUMNS" in sent and "'sys_user'" in sent

    def test_column_hint_survives_lookup_failure(self):
        """查列失败不能把提示变成异常，也不能吞掉原始错误。"""
        with patch.object(conn_pool, "get_connection", side_effect=Exception("boom")):
            out = sql_hints.augment_error(
                make_entry(), "SELECT bad FROM t", "Unknown column 'bad' in 'field list'")
        assert "Unknown column 'bad'" in out
        assert "实际列" not in out

    def test_column_hint_skips_unknown_table(self):
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.__enter__.return_value = cur
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value = cur
        with patch.object(conn_pool, "get_connection", return_value=conn):
            out = sql_hints.augment_error(
                make_entry(), "SELECT a FROM ghost", "Unknown column 'a' in 'field list'")
        assert "实际列" not in out

    def test_column_hint_caps_columns(self):
        cur = MagicMock()
        cur.fetchall.return_value = [(f"c{i}",) for i in range(100)]
        cur.__enter__.return_value = cur
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value = cur
        with patch.object(conn_pool, "get_connection", return_value=conn):
            out = sql_hints.augment_error(
                make_entry(), "SELECT bad FROM big", "Unknown column 'bad' in 'field list'")
        assert "共 100 列" in out

    def test_syntax_hint_mysql_backticks(self):
        out = sql_hints.augment_error(
            make_entry(), "SHOW COLUMNS FROM medical-my.consultation",
            "1064 (42000): You have an error in your SQL syntax; check the manual ...")
        assert "反引号" in out and "`medical-my`.`consultation`" in out

    def test_syntax_hint_dameng_quotes(self):
        out = sql_hints.augment_error(
            make_entry(db_type="dameng"), 'SELECT * FROM MY-SCHEMA.T',
            "Error in line: 1 syntax error")
        assert "反引号" not in out
        assert "双引号" in out

    def test_dameng_column_error_gets_hint(self):
        cur = MagicMock()
        cur.fetchall.return_value = [("ID",), ("DEL_FLAG",)]
        cur.__enter__.return_value = cur
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value = cur
        with patch.object(conn_pool, "get_connection", return_value=conn):
            out = sql_hints.augment_error(
                make_entry(db_type="dameng", database="DMHR"),
                "SELECT DEL_FLAG FROM EMP",
                "[CODE:-2111]Error in line: 1\nInvalid column name [DEL_FLAG]")
        assert "EMP 的实际列" in out
        sent = cur.execute.call_args.args[0]
        assert "ALL_TAB_COLUMNS" in sent and "UPPER(OWNER) = UPPER('DMHR')" in sent


# ---------------------------------------------------------------------------
# 工具路径端到端：错误信息带上提示
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg_db1(tmp_path, monkeypatch):
    monkeypatch.setattr(db_config, "CONFIG_FILE", tmp_path / "databases.json")
    monkeypatch.setattr(db_config, "CONFIG_DIR", tmp_path)
    for k in ("MYSQL_USER", "MYSQL_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    db_config.reset_cache()
    db_config.save_config({"default_alias": "db1", "databases": {"db1": make_entry()}})
    yield
    db_config.reset_cache()


class _Boom(Exception):
    def __init__(self, msg):
        super().__init__(msg)
        self.msg = msg


@pytest.mark.asyncio
async def test_execute_sql_error_includes_column_hint(cfg_db1):
    """查询报 Unknown column 时，返回文本里附上该表真实列。"""
    cols_cur = MagicMock()
    cols_cur.fetchall.return_value = [("id",), ("name",)]
    cols_cur.__enter__.return_value = cols_cur
    cols_conn = MagicMock()
    cols_conn.__enter__.return_value = cols_conn
    cols_conn.cursor.return_value = cols_cur

    calls = {"n": 0}

    def _get(entry, role, host=None, port=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Boom("Unknown column 'tenant_id' in 'field list'")
        return cols_conn

    with patch.object(conn_pool, "get_connection", side_effect=_get):
        r = await call_tool_impl("execute_sql",
                                 {"query": "SELECT tenant_id FROM sys_user"})
    text = text_of(r)
    assert "Unknown column 'tenant_id'" in text
    assert "sys_user 的实际列" in text
    assert "id, name" in text


@pytest.mark.asyncio
async def test_execute_sql_syntax_error_includes_quote_hint(cfg_db1):
    def _get(entry, role, host=None, port=None):
        raise _Boom("You have an error in your SQL syntax; check the manual that "
                    "corresponds to your MySQL server version for the right syntax to use "
                    "near 'medical-my.consultation' at line 1")

    with patch.object(conn_pool, "get_connection", side_effect=_get):
        r = await call_tool_impl("execute_sql",
                                 {"query": "SHOW COLUMNS FROM medical-my.consultation"})
    assert "反引号" in text_of(r)


@pytest.mark.asyncio
async def test_dameng_schema_retries_without_comments(cfg_db1):
    """达梦数据字典缺 COMMENTS 列时，自动降级重试不含注释的查询。"""
    from mysql_mcp_server.server import call_tool_impl as call
    cfg = db_config.load_config()
    cfg["databases"]["dm1"] = make_entry(db_type="dameng", database="DMHR", port=5236)
    db_config.save_config(cfg)

    seen = []

    class _Cur:
        description = [("COLUMN_NAME",), ("DATA_TYPE",), ("NULLABLE",), ("DATA_DEFAULT",)]
        def execute(self, sql):
            seen.append(sql)
            self._fail = "COMMENTS" in sql
        def fetchall(self):
            if getattr(self, "_fail", False):
                raise _Boom("Invalid column name [COMMENTS]")
            return [("ID", "INTEGER", "N", None)]
        def fetchmany(self, n):
            return self.fetchall()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    with patch.object(conn_pool, "get_connection", side_effect=lambda *a, **k: _Conn()):
        r = await call("get_schema_info", {"table_name": "EMP", "alias": "dm1"})
    assert len(seen) == 2, seen
    assert "COMMENTS" in seen[0] and "COMMENTS" not in seen[1]
    assert "ID" in text_of(r) and "Error executing" not in text_of(r)


# ---------------------------------------------------------------------------
# 保存时自动校验
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_config, "CONFIG_FILE", tmp_path / "databases.json")
    monkeypatch.setattr(db_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(audit, "LOG_DIR", tmp_path)
    for k in ("MYSQL_USER", "MYSQL_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    db_config.reset_cache()
    audit.clear()
    admin_api._on_change_callbacks[:] = []
    yield TestClient(admin_api.create_admin_app(), client=("127.0.0.1", 50000))
    admin_api._on_change_callbacks[:] = []
    db_config.reset_cache()


def payload(alias="db1"):
    return {"alias": alias, "host": "localhost", "port": 3306, "database": "mydb",
            "read_user": {"user": "r", "password": "rp"},
            "write_user": {"user": "w", "password": "wp"}}


class TestVerifyOnSave:
    def test_warning_when_db_missing(self, client, monkeypatch):
        monkeypatch.setenv("MYSQL_MCP_VERIFY_ON_SAVE", "true")
        with patch.object(admin_api, "probe_entry",
                          return_value="数据库 'mydb' 不存在或当前账号无权访问"):
            r = client.post("/api/databases", json=payload())
        assert r.status_code == 201
        assert "自动校验未通过" in r.json()["warning"]
        assert "不存在或当前账号无权访问" in r.json()["warning"]
        # 保存仍然生效
        assert "db1" in db_config.load_config()["databases"]

    def test_no_warning_when_ok(self, client, monkeypatch):
        monkeypatch.setenv("MYSQL_MCP_VERIFY_ON_SAVE", "true")
        with patch.object(admin_api, "probe_entry", return_value=None):
            r = client.post("/api/databases", json=payload())
        assert r.status_code == 201
        assert "warning" not in r.json()

    def test_disabled_by_env(self, client, monkeypatch):
        monkeypatch.setenv("MYSQL_MCP_VERIFY_ON_SAVE", "false")
        with patch.object(admin_api, "probe_entry", return_value="boom") as probe:
            r = client.post("/api/databases", json=payload())
        assert "warning" not in r.json()
        probe.assert_not_called()

    def test_update_database_returns_warning(self, client, monkeypatch):
        monkeypatch.setenv("MYSQL_MCP_VERIFY_ON_SAVE", "false")
        client.post("/api/databases", json=payload())
        monkeypatch.setenv("MYSQL_MCP_VERIFY_ON_SAVE", "true")
        with patch.object(admin_api, "probe_entry", return_value="实例连不上"):
            r = client.put("/api/databases/db1", json=payload())
        assert r.status_code == 200
        assert "实例连不上" in r.json()["warning"]

    def test_verify_expands_user_refs(self, client, monkeypatch):
        """引用模式下校验必须先展开用户账号，否则探测拿到空账号。"""
        monkeypatch.setenv("MYSQL_MCP_VERIFY_ON_SAVE", "true")
        cfg = db_config.load_config()
        cfg["users"] = {"ro": {"user": "real_reader", "password": "pw"}}
        db_config.save_config(cfg)
        captured = {}

        def _probe(entry, role="read"):
            captured.update(entry)
            return None

        with patch.object(admin_api, "probe_entry", side_effect=_probe):
            r = client.post("/api/databases", json={
                "alias": "db2", "host": "h", "database": "d",
                "read_user_ref": "ro", "write_user_ref": "ro"})
        assert r.status_code == 201 and "warning" not in r.json()
        assert captured["read_user"] == {"user": "real_reader", "password": "pw"}

    def test_dangling_ref_reported_as_warning(self, client, monkeypatch):
        monkeypatch.setenv("MYSQL_MCP_VERIFY_ON_SAVE", "true")
        cfg = db_config.load_config()
        cfg["users"] = {}
        db_config.save_config(cfg)
        with patch.object(admin_api, "probe_entry") as probe:
            # 引用不存在的用户会在 validate_entry 阶段就被拒（400），不会走到校验
            r = client.post("/api/databases", json={
                "alias": "db2", "host": "h", "read_user_ref": "ghost",
                "write_user_ref": "ghost"})
        assert r.status_code == 400
        probe.assert_not_called()

    def test_probe_failure_becomes_warning_not_500(self, client, monkeypatch):
        monkeypatch.setenv("MYSQL_MCP_VERIFY_ON_SAVE", "true")
        with patch.object(admin_api, "probe_entry", side_effect=RuntimeError("kaboom")):
            r = client.post("/api/databases", json=payload())
        assert r.status_code == 201
        assert "kaboom" in r.json()["warning"]
