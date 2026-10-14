"""用户维护功能测试：用户表 CRUD、引用校验/展开、改名级联、删除保护、
迁移内联账号、/api/users 路由、引用数据库的连接账号来源。"""

import pytest
from unittest.mock import MagicMock, patch
from starlette.testclient import TestClient

from mysql_mcp_server import admin_api, audit, db_config
from mysql_mcp_server.db_drivers import build_config
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


def seed(**kw):
    """写一份含 users + 引用配置的配置。"""
    cfg = {
        "default_alias": kw.get("default_alias", "db1"),
        "users": kw.get("users", {"reader1": {"user": "readonly_prod", "password": "rop"},
                                  "writer1": {"user": "rw_prod", "password": "rwp"}}),
        "databases": kw.get("databases", {
            "db1": make_entry(read_user_ref="reader1", write_user_ref="writer1",
                              read_user={"user": "", "password": ""},
                              write_user={"user": "", "password": ""}),
        }),
    }
    db_config.save_config(cfg)
    db_config.reset_cache()
    return cfg


# ---------------------------------------------------------------------------
# db_config：用户表 CRUD
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(db_config, "CONFIG_FILE", tmp_path / "databases.json")
    monkeypatch.setattr(db_config, "CONFIG_DIR", tmp_path)
    for k in ("MYSQL_USER", "MYSQL_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    db_config.reset_cache()
    seed()
    yield
    db_config.reset_cache()


class TestValidateUser:
    def test_empty_name(self):
        with pytest.raises(ValueError, match="名称不能为空"):
            db_config.validate_user("", {"user": "a"})
        with pytest.raises(ValueError, match="名称不能为空"):
            db_config.validate_user("   ", {"user": "a"})

    def test_too_long_name(self):
        with pytest.raises(ValueError, match="过长"):
            db_config.validate_user("x" * 65, {"user": "a"})

    def test_whitespace_or_control_chars(self):
        with pytest.raises(ValueError, match="控制字符"):
            db_config.validate_user("a\tb", {"user": "u"})
        with pytest.raises(ValueError, match="首尾空白"):
            db_config.validate_user(" ab ", {"user": "u"})

    def test_chinese_name_ok(self):
        db_config.validate_user("生产只读", {"user": "readonly_prod"})

    def test_user_required(self):
        with pytest.raises(ValueError, match="账号不能为空"):
            db_config.validate_user("ro", {"user": ""})
        with pytest.raises(ValueError, match="账号不能为空"):
            db_config.validate_user("ro", {})

    def test_empty_password_allowed(self):
        db_config.normalize_user({"user": "a", "password": ""})


class TestUserCrud:
    def test_create_and_read(self, cfg):
        db_config.create_user("ro2", {"user": "u2", "password": "p2"})
        assert db_config.get_user("ro2") == {"user": "u2", "password": "p2"}
        assert "ro2" in db_config.list_users()

    def test_create_duplicate_raises(self, cfg):
        with pytest.raises(ValueError, match="已存在"):
            db_config.create_user("reader1", {"user": "x", "password": "y"})

    def test_update_keeps_password_when_blank(self, cfg):
        db_config.update_user("reader1", "reader1", {"user": "newuser", "password": ""})
        assert db_config.get_user("reader1") == {"user": "newuser", "password": "rop"}

    def test_update_missing_raises(self, cfg):
        with pytest.raises(ValueError, match="不存在"):
            db_config.update_user("nope", "nope", {"user": "x"})

    def test_delete_missing_raises(self, cfg):
        with pytest.raises(ValueError, match="不存在"):
            db_config.delete_user("nope")

    def test_delete_blocked_by_reference(self, cfg):
        with pytest.raises(ValueError, match="正被 1 个数据库引用"):
            db_config.delete_user("reader1")

    def test_delete_ok_when_unreferenced(self, cfg):
        db_config.create_user("tmp", {"user": "t", "password": ""})
        db_config.delete_user("tmp")
        assert "tmp" not in db_config.list_users()

    def test_user_references_lists_roles(self, cfg):
        assert db_config.user_references("reader1") == ["db1"]
        assert db_config.user_references("writer1") == ["db1"]
        assert db_config.user_references("ghost") == []


# ---------------------------------------------------------------------------
# 改名级联
# ---------------------------------------------------------------------------

class TestRenameCascade:
    def test_rename_updates_all_refs(self, cfg):
        db_config.update_user("reader1", "只读账号", {"password": ""})
        cfg_after = db_config.load_config()
        assert "reader1" not in cfg_after["users"]
        assert "只读账号" in cfg_after["users"]
        entry = cfg_after["databases"]["db1"]
        assert entry["read_user_ref"] == "只读账号"
        assert entry["write_user_ref"] == "writer1"  # 写引用不受影响

    def test_rename_collision_raises(self, cfg):
        with pytest.raises(ValueError, match="已存在"):
            db_config.update_user("reader1", "writer1", {"password": ""})

    def test_rename_cascades_to_multiple_databases(self, cfg):
        cfg_now = db_config.load_config()
        for a in ("db2", "db3"):
            cfg_now["databases"][a] = make_entry(read_user_ref="reader1",
                                                 write_user_ref="writer1")
        db_config.save_config(cfg_now)
        db_config.update_user("reader1", "ro", {"password": ""})
        for a in ("db1", "db2", "db3"):
            assert db_config.load_config()["databases"][a]["read_user_ref"] == "ro"


# ---------------------------------------------------------------------------
# 引用校验 / 展开
# ---------------------------------------------------------------------------

class TestEntryRefs:
    def test_validate_entry_accepts_refs(self, cfg):
        cfg_cur = db_config.load_config()
        entry = cfg_cur["databases"]["db1"]
        db_config.validate_entry("db1", entry, cfg_cur)  # 不应抛错（账号来自引用）

    def test_validate_entry_rejects_dangling_ref(self, cfg):
        entry = make_entry(**{"read_user_ref": "ghost"})
        with pytest.raises(ValueError, match="read_user_ref 引用的用户"):
            db_config.validate_entry("db1", entry, db_config.load_config())

    def test_validate_entry_still_requires_inline_user(self, cfg):
        entry = make_entry()
        entry["read_user"] = {"user": "", "password": ""}
        with pytest.raises(ValueError, match="read_user"):
            db_config.validate_entry("db1", entry, db_config.load_config())

    def test_normalize_keeps_refs(self):
        entry = make_entry(read_user_ref="a", write_user_ref="b",
                           read_user={"user": "", "password": ""},
                           write_user={"user": "", "password": ""})
        out = db_config.normalize_entry(entry)
        assert out["read_user_ref"] == "a" and out["write_user_ref"] == "b"
        assert out["read_user"] == {"user": "", "password": ""}

    def test_normalize_blank_ref_becomes_none(self):
        out = db_config.normalize_entry(make_entry(**{"read_user_ref": "  "}))
        assert out["read_user_ref"] is None

    def test_expand_entry_fills_accounts(self, cfg):
        entry = dict(db_config.load_config()["databases"]["db1"])
        db_config.expand_entry(entry, db_config.load_config())
        assert entry["read_user"] == {"user": "readonly_prod", "password": "rop"}
        assert entry["write_user"] == {"user": "rw_prod", "password": "rwp"}
        assert "_account_error" not in entry

    def test_expand_entry_dangling_ref_sets_error(self, cfg):
        cfg_now = db_config.load_config()
        cfg_now["databases"]["db1"]["read_user_ref"] = "ghost"
        db_config.save_config(cfg_now)
        entry = dict(db_config.load_config()["databases"]["db1"])
        db_config.expand_entry(entry, db_config.load_config())
        assert "ghost" in entry["_account_error"]
        assert entry["read_user"] == {"user": "", "password": ""}

    def test_resolve_expands_accounts(self, cfg):
        alias, entry = db_config.resolve("db1")
        assert alias == "db1"
        assert entry["read_user"]["user"] == "readonly_prod"
        cfg_after = db_config.load_config()
        assert cfg_after["databases"]["db1"]["read_user"] == {"user": "", "password": ""}

    def test_build_config_uses_user_credentials(self, cfg):
        _, entry = db_config.resolve("db1")
        cfg_out = build_config(entry, "read")
        assert cfg_out["user"] == "readonly_prod"
        assert cfg_out["password"] == "rop"
        cfg_out = build_config(entry, "write")
        assert cfg_out["user"] == "rw_prod"

    def test_masked_config_hides_user_passwords(self, cfg):
        masked = db_config.masked_config(db_config.load_config())
        assert masked["users"]["reader1"]["password"] == "****"
        assert db_config.load_config()["users"]["reader1"]["password"] == "rop"

    def test_legacy_entry_without_users_key(self, cfg):
        db_config.save_config({"default_alias": "old",
                               "databases": {"old": make_entry()}})
        db_config.reset_cache()
        cfg_cur = db_config.load_config()
        assert cfg_cur["users"] == {}  # 兼容：旧文件没有 users
        _, entry = db_config.resolve("old")
        assert entry["read_user"]["user"] == "reader"  # 内联模式照常


# ---------------------------------------------------------------------------
# 迁移内联账号
# ---------------------------------------------------------------------------

class TestMigrate:
    def test_migrate_creates_users_and_refs(self, cfg):
        cfg_now = db_config.load_config()
        cfg_now["databases"]["db1"] = make_entry()  # 内联 reader/writer
        db_config.save_config(cfg_now)
        result = db_config.migrate_inline_accounts()
        assert set(result["created"]) == {"reader", "writer"}
        cfg_after = db_config.load_config()
        assert cfg_after["databases"]["db1"]["read_user_ref"] == "reader"
        assert cfg_after["databases"]["db1"]["write_user_ref"] == "writer"
        assert cfg_after["users"]["reader"] == {"user": "reader", "password": "rp"}

    def test_migrate_is_idempotent_for_referenced(self, cfg):
        result = db_config.migrate_inline_accounts()
        assert result["created"] == [] and result["updated"] == []
        assert db_config.load_config()["databases"]["db1"]["read_user_ref"] == "reader1"

    def test_migrate_merges_same_account(self, cfg):
        cfg_now = db_config.load_config()
        cfg_now["databases"]["db1"] = make_entry(write_user={"user": "reader", "password": "rp"})
        db_config.save_config(cfg_now)
        result = db_config.migrate_inline_accounts()
        assert result["created"] == ["reader"]  # 读写同账号合并为一个用户
        cfg_after = db_config.load_config()
        assert cfg_after["databases"]["db1"]["write_user_ref"] == "reader"

    def test_migrate_skips_entries_without_accounts(self, cfg):
        cfg_now = db_config.load_config()
        cfg_now["databases"]["db1"] = make_entry(read_user={"user": "", "password": ""},
                                                 write_user={"user": "", "password": ""})
        db_config.save_config(cfg_now)
        result = db_config.migrate_inline_accounts()
        assert result["created"] == [] and result["skipped"] == 1


# ---------------------------------------------------------------------------
# admin API：/api/users
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_config, "CONFIG_FILE", tmp_path / "databases.json")
    monkeypatch.setattr(db_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(audit, "LOG_DIR", tmp_path)
    monkeypatch.setenv("MYSQL_MCP_VERIFY_ON_SAVE", "false")
    for k in ("MYSQL_USER", "MYSQL_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    db_config.reset_cache()
    audit.clear()
    admin_api._on_change_callbacks[:] = []
    seed()
    conn_pool.clear_all()
    yield TestClient(admin_api.create_admin_app(), client=("127.0.0.1", 50000))
    admin_api._on_change_callbacks[:] = []
    db_config.reset_cache()


class TestUsersApi:
    def test_list_masks_password_and_shows_refs(self, client):
        r = client.get("/api/users")
        assert r.status_code == 200
        users = {u["name"]: u for u in r.json()["users"]}
        assert users["reader1"]["user"] == "readonly_prod"
        assert users["reader1"]["password"] == "****"
        assert users["reader1"]["references"] == ["db1"]

    def test_create_user(self, client):
        r = client.post("/api/users", json={"name": "ro3", "user": "u3", "password": "p3"})
        assert r.status_code == 201 and r.json()["name"] == "ro3"
        names = [u["name"] for u in client.get("/api/users").json()["users"]]
        assert "ro3" in names

    def test_create_duplicate_409(self, client):
        r = client.post("/api/users", json={"name": "reader1", "user": "x", "password": "y"})
        assert r.status_code == 409

    def test_create_invalid_400(self, client):
        r = client.post("/api/users", json={"name": "n", "user": "", "password": ""})
        assert r.status_code == 400 and "账号" in r.json()["error"]

    def test_update_user_and_rename_cascades(self, client):
        r = client.put("/api/users/reader1", json={"name": "ro", "user": "u9", "password": ""})
        assert r.status_code == 200 and r.json()["renamed"] is True
        cfg = db_config.load_config()
        assert cfg["databases"]["db1"]["read_user_ref"] == "ro"
        users = {u["name"]: u for u in client.get("/api/users").json()["users"]}
        assert users["ro"]["user"] == "u9"

    def test_update_rename_collision_409(self, client):
        r = client.put("/api/users/reader1", json={"name": "writer1", "password": ""})
        assert r.status_code == 409

    def test_update_missing_404(self, client):
        assert client.put("/api/users/ghost", json={"password": ""}).status_code == 404

    def test_delete_blocked_with_references(self, client):
        r = client.delete("/api/users/reader1")
        assert r.status_code == 409
        assert r.json()["references"] == ["db1"]

    def test_delete_force_clears_refs(self, client):
        r = client.delete("/api/users/reader1?force=true")
        assert r.status_code == 200 and r.json()["force_cleared"] == 1
        cfg = db_config.load_config()
        assert cfg["databases"]["db1"]["read_user_ref"] is None
        assert "reader1" not in cfg["users"]

    def test_delete_missing_404(self, client):
        assert client.delete("/api/users/ghost").status_code == 404

    def test_migrate_endpoint(self, client):
        cfg = db_config.load_config()
        cfg["databases"]["db1"] = make_entry()
        db_config.save_config(cfg)
        r = client.post("/api/users/migrate", json={})
        assert r.status_code == 200
        assert set(r.json()["created"]) == {"reader", "writer"}
        assert db_config.load_config()["databases"]["db1"]["read_user_ref"] == "reader"

    def test_users_endpoint_guarded_for_lan(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db_config, "CONFIG_FILE", tmp_path / "databases.json")
        monkeypatch.setattr(db_config, "CONFIG_DIR", tmp_path)
        db_config.reset_cache()
        seed()
        c = TestClient(admin_api.create_admin_app(), client=("10.0.0.5", 50000))
        assert c.get("/api/users").status_code == 403
        db_config.reset_cache()


class TestDatabaseRefFlow:
    def test_create_database_with_user_refs(self, client):
        payload = {"alias": "db2", "host": "h", "port": 3306, "database": "d",
                   "read_user_ref": "reader1", "write_user_ref": "writer1"}
        r = client.post("/api/databases", json=payload)
        assert r.status_code == 201
        _, entry = db_config.resolve("db2")
        assert entry["read_user"]["user"] == "readonly_prod"

    def test_create_database_dangling_ref_400(self, client):
        payload = {"alias": "db2", "host": "h", "read_user_ref": "ghost",
                   "write_user_ref": "writer1"}
        r = client.post("/api/databases", json=payload)
        assert r.status_code == 400 and "用户" in r.json()["error"]

    def test_switch_inline_to_ref_keeps_working(self, client):
        payload = {"alias": "db2", "host": "h", "read_user_ref": "reader1",
                   "write_user_ref": "writer1"}
        client.post("/api/databases", json=payload)
        cfg = db_config.load_config()
        cfg["databases"]["db2"]["read_user_ref"] = None
        cfg["databases"]["db2"]["read_user"] = {"user": "inline_u", "password": "inline_p"}
        db_config.save_config(cfg)
        client.put("/api/databases/db2", json={**payload, "read_user_ref": "writer1"})
        _, entry = db_config.resolve("db2")
        assert entry["read_user"]["user"] == "rw_prod"

    def test_import_with_users_and_refs(self, client):
        cfg = {"default_alias": "db1",
               "users": {"u1": {"user": "ur", "password": "pr"}},
               "databases": {"db1": make_entry(read_user_ref="u1", write_user_ref="u1")}}
        r = client.post("/api/databases/import", json=cfg)
        assert r.status_code == 200 and r.json()["users"] == 1
        assert db_config.load_config()["databases"]["db1"]["read_user_ref"] == "u1"

    def test_import_dangling_ref_rejected(self, client):
        cfg = {"default_alias": "db1", "users": {},
               "databases": {"db1": make_entry(read_user_ref="ghost")}}
        r = client.post("/api/databases/import", json=cfg)
        assert r.status_code == 400 and "ghost" in r.json()["error"]

    def test_export_includes_users_with_passwords(self, client):
        r = client.get("/api/databases/export")
        assert r.json()["users"]["reader1"]["password"] == "rop"

    def test_health_counts_users(self, client):
        assert client.get("/api/health").json()["users"] == 2


# ---------------------------------------------------------------------------
# 工具路径：悬空引用给出可操作提示
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_call_dangling_ref_hint(tmp_path, monkeypatch, cfg):
    conn, cur = fake_conn(rows=[(1,)], columns=("N",))
    cfg_now = db_config.load_config()
    cfg_now["databases"]["db1"]["read_user_ref"] = "ghost"
    db_config.save_config(cfg_now)
    with patch.object(conn_pool, "get_connection", return_value=conn):
        r = await call_tool_impl("execute_sql", {"query": "SELECT 1"})
    text = text_of(r)
    assert "用户「ghost」不存在" in text
    assert "管理页面" in text
    cur.execute.assert_not_called()  # 不应真正连接


@pytest.mark.asyncio
async def test_tool_call_uses_referenced_user(tmp_path, monkeypatch, cfg):
    conn, cur = fake_conn(rows=[(1,)], columns=("N",))
    seen = {}

    def _get(entry, role, host=None, port=None):
        seen.update(build_config(entry, role))
        return conn

    with patch.object(conn_pool, "get_connection", side_effect=_get):
        r = await call_tool_impl("execute_sql", {"query": "SELECT 1"})
    assert "1" in text_of(r)
    assert seen["user"] == "readonly_prod"
    assert seen["password"] == "rop"
