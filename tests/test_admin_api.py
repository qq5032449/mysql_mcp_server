import json
import pytest
from unittest.mock import patch, MagicMock
from starlette.testclient import TestClient

from mysql_mcp_server import admin_api, audit, db_config


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(db_config, "CONFIG_FILE", tmp_path / "databases.json")
    monkeypatch.setattr(db_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(audit, "LOG_DIR", tmp_path)
    db_config.reset_cache()
    audit.clear()
    yield admin_api.create_admin_app()
    db_config.reset_cache()


@pytest.fixture
def client(app):
    # 回环校验：TestClient 默认 client host 是 "testclient"，必须显式传 127.0.0.1
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def no_env(monkeypatch):
    for k in ("MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE"):
        monkeypatch.delenv(k, raising=False)


def payload(alias="db1"):
    return {
        "alias": alias, "host": "localhost", "port": 3306, "database": "mydb",
        "read_user": {"user": "r", "password": "rp"},
        "write_user": {"user": "w", "password": "wp"},
    }


class TestLoopbackGuard:
    def test_non_loopback_forbidden(self, app, no_env):
        c = TestClient(app, client=("10.0.0.5", 50000))
        r = c.get("/api/databases")
        assert r.status_code == 403


class TestCrud:
    def test_empty_list(self, client, no_env):
        r = client.get("/api/databases")
        assert r.status_code == 200
        assert r.json() == {"default_alias": None, "databases": {}}

    def test_create_and_mask(self, client, no_env):
        r = client.post("/api/databases", json=payload())
        assert r.status_code == 201
        r = client.get("/api/databases")
        body = r.json()
        assert body["default_alias"] == "db1"
        assert body["databases"]["db1"]["read_user"]["password"] == "****"
        assert body["databases"]["db1"]["write_user"]["password"] == "****"

    def test_create_duplicate_409(self, client, no_env):
        client.post("/api/databases", json=payload())
        r = client.post("/api/databases", json=payload())
        assert r.status_code == 409

    def test_create_invalid_400(self, client, no_env):
        r = client.post("/api/databases", json=payload(alias="bad alias!"))
        assert r.status_code == 400
        assert "别名" in r.json()["error"]

    def test_update_keeps_password_when_blank(self, client, no_env):
        client.post("/api/databases", json=payload())
        body = payload()
        body["read_user"]["password"] = ""
        body["write_user"]["password"] = ""
        r = client.put("/api/databases/db1", json=body)
        assert r.status_code == 200
        cfg = db_config.load_config()
        assert cfg["databases"]["db1"]["read_user"]["password"] == "rp"
        assert cfg["databases"]["db1"]["write_user"]["password"] == "wp"

    def test_update_changes_password(self, client, no_env):
        client.post("/api/databases", json=payload())
        body = payload()
        body["write_user"]["password"] = "new"
        r = client.put("/api/databases/db1", json=body)
        assert r.status_code == 200
        assert db_config.load_config()["databases"]["db1"]["write_user"]["password"] == "new"

    def test_delete_last_clears_default(self, client, no_env):
        client.post("/api/databases", json=payload())
        r = client.delete("/api/databases/db1")
        assert r.status_code == 200
        body = client.get("/api/databases").json()
        assert body == {"default_alias": None, "databases": {}}

    def test_delete_default_switches_to_remaining(self, client, no_env):
        client.post("/api/databases", json=payload("db1"))
        client.post("/api/databases", json=payload("db2"))
        client.delete("/api/databases/db1")
        body = client.get("/api/databases").json()
        assert body["default_alias"] == "db2"

    def test_delete_missing_404(self, client, no_env):
        assert client.delete("/api/databases/nope").status_code == 404


class TestSettings:
    def test_set_default(self, client, no_env):
        client.post("/api/databases", json=payload("db1"))
        client.post("/api/databases", json=payload("db2"))
        r = client.put("/api/settings", json={"default_alias": "db2"})
        assert r.status_code == 200
        assert client.get("/api/databases").json()["default_alias"] == "db2"

    def test_set_default_missing_alias_400(self, client, no_env):
        client.post("/api/databases", json=payload("db1"))
        assert client.put("/api/settings", json={"default_alias": "zzz"}).status_code == 400


class TestConnection:
    def test_read_ok(self, client, no_env):
        client.post("/api/databases", json=payload())
        with patch("mysql_mcp_server.admin_api.mysql.connector.connect") as m:
            m.return_value = MagicMock()
            r = client.post("/api/databases/db1/test", json={})
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        kwargs = m.call_args.kwargs
        assert kwargs["user"] == "r"
        assert kwargs["password"] == "rp"

    def test_write_account(self, client, no_env):
        client.post("/api/databases", json=payload())
        with patch("mysql_mcp_server.admin_api.mysql.connector.connect") as m:
            m.return_value = MagicMock()
            r = client.post("/api/databases/db1/test", json={"as_write": True})
        assert r.status_code == 200
        assert m.call_args.kwargs["user"] == "w"

    def test_failure_detail(self, client, no_env):
        client.post("/api/databases", json=payload())
        err = MagicMock()
        err.msg = "Access denied"
        with patch("mysql_mcp_server.admin_api.mysql.connector.connect",
                   side_effect=type("E", (Exception,), {"msg": "Access denied"})):
            r = client.post("/api/databases/db1/test", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert "Access denied" in body["error"]


class TestAuditHealth:
    def test_audit_listed(self, client, no_env):
        audit.record("db1", "UPDATE", "client", "UPDATE t SET x=1")
        r = client.get("/api/audit")
        assert r.status_code == 200
        assert r.json()[0]["alias"] == "db1"

    def test_health(self, client, no_env):
        client.post("/api/databases", json=payload())
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert r.json()["databases"] == 1
