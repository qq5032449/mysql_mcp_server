import json
import pytest
from mysql_mcp_server import db_config


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """把配置文件指向临时目录并清缓存。"""
    f = tmp_path / "databases.json"
    monkeypatch.setattr(db_config, "CONFIG_FILE", f)
    monkeypatch.setattr(db_config, "CONFIG_DIR", tmp_path)
    db_config.reset_cache()
    yield f
    db_config.reset_cache()


@pytest.fixture
def no_env(monkeypatch):
    for k in ("MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE"):
        monkeypatch.delenv(k, raising=False)


def entry(alias="db1", **kw):
    base = {
        "host": "localhost",
        "port": 3306,
        "database": "mydb",
        "charset": "utf8mb4",
        "read_user": {"user": "reader", "password": "rp"},
        "write_user": {"user": "writer", "password": "wp"},
        "write_policy": "client_confirm",
        "allow_delete": False,
    }
    base.update(kw)
    return base


class TestLoad:
    def test_missing_file_returns_empty(self, cfg_file, no_env):
        cfg = db_config.load_config()
        assert cfg == {"default_alias": None, "databases": {}}

    def test_load_existing(self, cfg_file, no_env):
        cfg_file.write_text(json.dumps({"default_alias": "db1", "databases": {"db1": entry()}}), encoding="utf-8")
        cfg = db_config.load_config()
        assert cfg["default_alias"] == "db1"
        assert cfg["databases"]["db1"]["read_user"]["user"] == "reader"

    def test_load_corrupt_file_returns_empty(self, cfg_file, no_env):
        cfg_file.write_text("{not json", encoding="utf-8")
        assert db_config.load_config() == {"default_alias": None, "databases": {}}


class TestEnvFallback:
    def test_env_creates_default_entry(self, cfg_file, monkeypatch):
        monkeypatch.setenv("MYSQL_USER", "u")
        monkeypatch.setenv("MYSQL_PASSWORD", "p")
        cfg = db_config.load_config()
        assert "default" in cfg["databases"]
        assert cfg["default_alias"] == "default"
        e = cfg["databases"]["default"]
        assert e["read_user"] == {"user": "u", "password": "p"}
        assert e["write_user"] == {"user": "u", "password": "p"}

    def test_json_wins_over_env(self, cfg_file, no_env, monkeypatch):
        monkeypatch.setenv("MYSQL_USER", "u")
        monkeypatch.setenv("MYSQL_PASSWORD", "p")
        cfg_file.write_text(json.dumps({"default_alias": "db1", "databases": {"db1": entry()}}), encoding="utf-8")
        cfg = db_config.load_config()
        assert set(cfg["databases"]) == {"db1"}


class TestSave:
    def test_save_then_load_roundtrip(self, cfg_file, no_env):
        db_config.save_config({"default_alias": "a", "databases": {"a": entry("a")}})
        assert db_config.load_config()["databases"]["a"]["host"] == "localhost"

    def test_save_atomic_no_tmp_left(self, cfg_file, no_env):
        db_config.save_config({"default_alias": "a", "databases": {"a": entry("a")}})
        leftovers = [p for p in cfg_file.parent.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []
        assert cfg_file.exists()


class TestValidate:
    def test_ok(self, no_env):
        db_config.validate_entry("db1", entry())

    def test_bad_alias_chars(self, no_env):
        with pytest.raises(ValueError, match="别名"):
            db_config.validate_entry("db 1", entry())
        with pytest.raises(ValueError, match="别名"):
            db_config.validate_entry("", entry())

    def test_missing_host(self, no_env):
        with pytest.raises(ValueError, match="host"):
            db_config.validate_entry("db1", entry(host=""))

    def test_bad_port(self, no_env):
        with pytest.raises(ValueError, match="port"):
            db_config.validate_entry("db1", entry(port=99999))
        with pytest.raises(ValueError, match="port"):
            db_config.validate_entry("db1", entry(port="x"))

    def test_missing_users(self, no_env):
        bad = entry(read_user={"user": "", "password": ""})
        with pytest.raises(ValueError, match="read_user"):
            db_config.validate_entry("db1", bad)

    def test_bad_write_policy(self, no_env):
        with pytest.raises(ValueError, match="write_policy"):
            db_config.validate_entry("db1", entry(write_policy="always"))


class TestNormalize:
    def test_defaults_and_whitelist(self, no_env):
        raw = dict(entry(), unknown_field="x", ssh={"enable": True})
        out = db_config.normalize_entry(raw)
        assert "unknown_field" not in out
        assert out["ssh"]["enable"] is True
        assert out["charset"] == "utf8mb4"
        assert out["write_policy"] == "client_confirm"
        assert out["allow_delete"] is False
        assert out["port"] == 3306


class TestResolve:
    def test_explicit_alias(self, cfg_file, no_env):
        db_config.save_config({"default_alias": "db1", "databases": {"db1": entry(), "db2": entry("db2")}})
        alias, e = db_config.resolve("db2")
        assert alias == "db2"

    def test_explicit_alias_missing(self, cfg_file, no_env):
        db_config.save_config({"default_alias": "db1", "databases": {"db1": entry()}})
        assert db_config.resolve("nope") is None

    def test_none_uses_default(self, cfg_file, no_env):
        db_config.save_config({"default_alias": "db2", "databases": {"db1": entry(), "db2": entry("db2")}})
        assert db_config.resolve(None)[0] == "db2"

    def test_none_without_default(self, cfg_file, no_env):
        db_config.save_config({"default_alias": None, "databases": {"db1": entry()}})
        assert db_config.resolve(None) is None

    def test_nothing_configured(self, cfg_file, no_env):
        assert db_config.resolve(None) is None


class TestMasked:
    def test_passwords_masked(self, no_env):
        cfg = {"default_alias": "db1", "databases": {"db1": entry()}}
        out = db_config.masked_config(cfg)
        e = out["databases"]["db1"]
        assert e["read_user"]["password"] == "****"
        assert e["write_user"]["password"] == "****"
        # 原配置不被修改
        assert cfg["databases"]["db1"]["read_user"]["password"] == "rp"
