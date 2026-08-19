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
    admin_api._on_change_callbacks[:] = []
    yield admin_api.create_admin_app()
    admin_api._on_change_callbacks[:] = []
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


class TestLanTokenAccess:
    """ADMIN_TOKEN 设置后，非回环客户端持令牌可访问，否则 401。"""

    def test_lan_without_token_401(self, app, monkeypatch, no_env):
        monkeypatch.setattr(admin_api, "_ADMIN_TOKEN", "secret123")
        c = TestClient(app, client=("192.168.84.5", 50000))
        assert c.get("/api/databases").status_code == 401

    def test_lan_with_valid_token_header(self, app, monkeypatch, no_env):
        monkeypatch.setattr(admin_api, "_ADMIN_TOKEN", "secret123")
        c = TestClient(app, client=("192.168.84.5", 50000))
        r = c.get("/api/databases", headers={"X-Admin-Token": "secret123"})
        assert r.status_code == 200

    def test_lan_with_valid_token_query(self, app, monkeypatch, no_env):
        monkeypatch.setattr(admin_api, "_ADMIN_TOKEN", "secret123")
        c = TestClient(app, client=("192.168.84.5", 50000))
        r = c.get("/api/databases?admin_token=secret123")
        assert r.status_code == 200

    def test_lan_with_wrong_token_401(self, app, monkeypatch, no_env):
        monkeypatch.setattr(admin_api, "_ADMIN_TOKEN", "secret123")
        c = TestClient(app, client=("192.168.84.5", 50000))
        assert c.get("/api/databases", headers={"X-Admin-Token": "bad"}).status_code == 401

    def test_loopback_still_token_free(self, app, monkeypatch, no_env):
        monkeypatch.setattr(admin_api, "_ADMIN_TOKEN", "secret123")
        c = TestClient(app, client=("127.0.0.1", 50000))
        assert c.get("/api/databases").status_code == 200

    def test_lan_static_page_served_without_token(self, app, monkeypatch, no_env):
        """静态页本身放行（无敏感数据），由前端在 401 后引导输入令牌。"""
        monkeypatch.setattr(admin_api, "_ADMIN_TOKEN", "secret123")
        c = TestClient(app, client=("192.168.84.5", 50000))
        r = c.get("/")
        assert r.status_code == 200


class TestHostAllowed:
    def test_ip_host_allowed(self):
        assert admin_api._host_allowed("192.168.84.22:8000") is True
        assert admin_api._host_allowed("[::1]:8000") is True

    def test_domain_host_rejected(self):
        assert admin_api._host_allowed("evil.example.com:8000") is False
        assert admin_api._host_allowed("evil.example.com") is False

    def test_localhost_and_testserver_allowed(self):
        assert admin_api._host_allowed("localhost:8000") is True
        assert admin_api._host_allowed("testserver") is True

    def test_missing_host_allowed(self):
        assert admin_api._host_allowed(None) is True


class TestOnChange:
    def test_callbacks_called_on_create_update_delete(self, client, no_env):
        calls = []
        admin_api.register_on_change(lambda alias: calls.append(alias))
        client.post("/api/databases", json=payload("db1"))
        client.put("/api/databases/db1", json=payload("db1"))
        client.delete("/api/databases/db1")
        assert calls == ["db1", "db1", "db1"]

    def test_callback_exception_does_not_break_api(self, client, no_env):
        def bad_cb(alias):
            raise RuntimeError("boom")
        admin_api.register_on_change(bad_cb)
        r = client.post("/api/databases", json=payload("db9"))
        assert r.status_code == 201

    def test_malformed_ssh_port_returns_400(self, client, no_env):
        body = payload()
        body["ssh"] = {"enable": True, "host": "h", "port": "abc"}
        r = client.post("/api/databases", json=body)
        assert r.status_code == 400
        assert "ssh 端口" in r.json()["error"]


class TestRegisterDedup:
    def test_register_same_callback_twice_called_once(self, client, no_env):
        calls = []
        cb = lambda alias: calls.append(alias)
        admin_api.register_on_change(cb)
        admin_api.register_on_change(cb)
        client.post("/api/databases", json=payload("dbX"))
        assert calls == ["dbX"]


class TestMiddleware:
    def test_static_page_open_but_api_guarded(self, app, no_env):
        """局域网：静态页放行（无敏感数据），API 无令牌被拦。"""
        c = TestClient(app, client=("10.0.0.5", 50000))
        assert c.get("/api/health").status_code == 403  # 无 ADMIN_TOKEN：仅回环
        # 静态页不在 /api 下，不做令牌校验（此路径不存在文件 → 404，非 403）
        assert c.get("/nonexistent-static").status_code == 404

    def test_bad_host_header_rejected(self, client, no_env):
        r = client.get("/api/health", headers={"Host": "evil.example.com"})
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

    def test_set_default_notifies_change(self, client, no_env):
        client.post("/api/databases", json=payload("db1"))
        client.post("/api/databases", json=payload("db2"))
        calls = []
        admin_api.register_on_change(lambda alias: calls.append(alias))
        r = client.put("/api/settings", json={"default_alias": "db2"})
        assert r.status_code == 200
        assert calls == ["db2"]


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


class TestAdminPage:
    def test_admin_index_served(self, client, no_env):
        r = client.get("/")
        assert r.status_code == 200
        assert "MySQL MCP 管理页面" in r.text
