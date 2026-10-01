import sqlite3

import pytest

from mysql_mcp_server import error_log


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(error_log, "DB_FILE", tmp_path / "error_log.db")
    monkeypatch.setattr(error_log, "FALLBACK_FILE", tmp_path / "error_log_fallback.log")
    error_log.clear()
    yield


def test_record_persists_and_lists():
    entry = error_log.record("db1", "execute_sql", "SELECT * FROM missing_table",
                             "Table 'mydb.missing_table' doesn't exist",
                             sql_type="SELECT", role="read")
    assert entry["alias"] == "db1"
    assert entry["tool"] == "execute_sql"
    assert entry["sql"] == "SELECT * FROM missing_table"
    assert entry["db_error"] == "Table 'mydb.missing_table' doesn't exist"
    assert entry["id"]

    rows = error_log.query_errors()
    assert len(rows) == 1
    assert rows[0]["sql"] == "SELECT * FROM missing_table"
    assert rows[0]["db_error"] == "Table 'mydb.missing_table' doesn't exist"


def test_permission_denied_update_delete_not_recorded():
    for msg in ("UPDATE command denied to user 'w'@'%' for table 't'",
                "DELETE command denied to user 'w'@'%' for table 't'"):
        assert error_log.record("db1", "execute_sql", "UPDATE t SET x=1", msg) is None
    assert error_log.query_errors() == []
    assert error_log.list_entries() == []


def test_non_permission_denied_still_recorded():
    entry = error_log.record("db1", "execute_sql", "SELECT 1", "Access denied for user 'x'")
    assert entry is not None
    assert len(error_log.query_errors()) == 1


def test_is_permission_error_cases():
    assert error_log.is_permission_error("UPDATE command denied to user 'w'@'%' for table 't'")
    assert error_log.is_permission_error("DELETE COMMAND DENIED to user")
    assert not error_log.is_permission_error("SELECT command denied to user")
    assert not error_log.is_permission_error("Table 't' doesn't exist")
    assert not error_log.is_permission_error("")


def test_query_latest_first_and_alias_filter():
    error_log.record("db1", "execute_sql", "SELECT 1", "boom1")
    error_log.record("db2", "execute_sql", "SELECT 2", "boom2")
    rows = error_log.query_errors()
    assert [r["alias"] for r in rows] == ["db2", "db1"]
    only_db1 = error_log.query_errors(alias="db1")
    assert [r["alias"] for r in only_db1] == ["db1"]


def test_db_failure_falls_back_to_memory_and_file(tmp_path, monkeypatch):
    monkeypatch.setattr(error_log, "DB_FILE", tmp_path)  # 目录即文件：连接必失败
    entry = error_log.record("db1", "execute_sql", "SELECT 1", "boom")
    assert entry is not None
    assert "boom" in error_log.FALLBACK_FILE.read_text(encoding="utf-8")
    assert error_log.query_errors()[0]["db_error"] == "boom"


def test_truncation():
    entry = error_log.record("db1", "execute_sql", "x" * 5000, "y" * 5000)
    assert len(entry["sql"]) == error_log.SQL_MAX_LEN
    assert len(entry["db_error"]) == error_log.ERR_MAX_LEN


def test_table_schema(tmp_path):
    path = tmp_path / "error_log.db"
    conn = sqlite3.connect(str(path))
    try:
        error_log._ensure_schema(conn)
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({error_log.TABLE_NAME})").fetchall()}
    finally:
        conn.close()
    assert {"id", "time", "alias", "tool", "sql_type", "role", "sql", "db_error"} <= cols
