import pytest
from mysql_mcp_server import audit


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_DIR", tmp_path)
    audit.clear()
    yield


def test_record_and_list():
    audit.record("db1", "DELETE", "elicitation", "DELETE FROM t")
    entries = audit.list_entries()
    assert len(entries) == 1
    e = entries[0]
    assert e["alias"] == "db1"
    assert e["type"] == "DELETE"
    assert e["channel"] == "elicitation"
    assert e["status"] == "executed"
    assert e["sql"] == "DELETE FROM t"
    assert e["time"]  # 非空时间戳


def test_cap_200():
    for i in range(250):
        audit.record("db1", "UPDATE", "client", f"UPDATE t SET x={i}")
    assert len(audit.list_entries()) == 200
    assert audit.list_entries()[-1]["sql"] == "UPDATE t SET x=249"


def test_record_fields_elapsed_rows():
    e = audit.record("db1", "UPDATE", "token", "UPDATE t SET x=1",
                     status="error", elapsed_ms=123, rows=None)
    assert e["status"] == "error"
    assert e["elapsed_ms"] == 123
    assert e["rows"] is None
    e2 = audit.record("db1", "INSERT", "skip_confirm", "INSERT INTO t VALUES (1)",
                      elapsed_ms=5, rows=1)
    assert e2["rows"] == 1


def test_file_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "MAX_FILE_BYTES", 200)
    for i in range(20):
        audit.record("db1", "UPDATE", "client", f"UPDATE t SET x={i} where pad-padding-pad")
    assert (tmp_path / "audit.log.1").exists()  # 第一代轮转文件已生成
    assert (tmp_path / "audit.log").exists()


def test_sql_truncated_to_200():
    audit.record("db1", "UPDATE", "client", "x" * 500)
    assert len(audit.list_entries()[0]["sql"]) == 200


def test_file_written():
    audit.record("db1", "DELETE", "elicitation", "DELETE FROM t")
    log_file = audit.LOG_DIR / "audit.log"
    assert log_file.exists()
    assert "DELETE FROM t" in log_file.read_text(encoding="utf-8")
