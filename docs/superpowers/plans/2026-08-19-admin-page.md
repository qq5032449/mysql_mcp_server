# MySQL MCP 管理页面与双账号执行 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为 mysql_mcp_server 增加 Web 管理页面（多数据库别名配置、读/写双账号、删除权限开关）与 SSE 别名路由，写操作经 MCP elicitation 确认后用写账号执行。

**架构：** 单进程 Starlette 服务：`/sse?alias=X` 按别名懒创建独立的 `SseServerTransport` + `Server` 实例；SQL 三级判定（读/写/删除）后读操作走 read_user 直连，写/删除经双通道确认（elicitation 优先，`client_confirm` 策略降级）走 write_user；配置存 `config/databases.json`（原子写+内存缓存），管理 API 与静态页面挂载在同进程。

**技术栈：** Python 3.12、mcp 1.29.0（低级 Server API）、Starlette 1.6、mysql-connector-python、pytest + pytest-asyncio（mode=auto）、starlette TestClient（httpx 已随 mcp 安装）。

**规格：** `docs/superpowers/specs/2026-08-19-admin-page-design.md`

**关键 SDK 事实（已在 .venv mcp 1.29.0 源码确认，实现时直接使用）：**
- `mcp.server.lowlevel.server.request_ctx` 是**模块级 contextvar**，所有 Server 实例共享；handler 内 `request_ctx.get()` 取 `RequestContext`
- `RequestContext.session.elicit(message, requestedSchema: dict) -> ElicitResult`，`ElicitResult.action ∈ {"accept","decline","cancel"}`
- 能力检测：`session.client_params.capabilities.elicitation` 非空即支持
- `Server` 装饰器（`list_tools()` 等）**返回原函数**，可写 `s.list_tools()(fn)` 手动注册
- `SseServerTransport(endpoint, security_settings=...)`；`connect_sse(scope, receive, send)`；`handle_post_message(scope, receive, send)` 是完整 ASGI app
- Starlette `Route(path, endpoint=<带 __call__ 的实例>)` 把 endpoint 当 ASGI app 处理（不走 request_response 包装）

**工作目录：** 所有命令在 `e:\coding\mysql\mysql_mcp_server` 下执行。pytest 用 `.venv\Scripts\python.exe -m pytest`。

**文件结构总览：**

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/mysql_mcp_server/sql_classify.py` | SQL 三级判定（读/写/删除），CTE 与注释处理 | 创建 |
| `src/mysql_mcp_server/db_config.py` | databases.json 加载/校验/原子保存/别名解析/env 兼容/脱敏 | 创建 |
| `src/mysql_mcp_server/audit.py` | 写操作审计（内存 100 条 + 文件追加） | 创建 |
| `src/mysql_mcp_server/admin_api.py` | 管理 REST API + 静态页挂载 + 回环校验 + 配置变更回调 | 创建 |
| `src/mysql_mcp_server/static/index.html` | 管理页面单文件前端 | 创建 |
| `src/mysql_mcp_server/server.py` | 改造：双账号执行、三级判定接入、elicitation、别名路由、挂载 admin | 修改 |
| `tests/test_classify.py` `tests/test_db_config.py` `tests/test_audit.py` `tests/test_admin_api.py` `tests/test_execute_policy.py` | 新模块测试 | 创建 |
| `tests/test_server.py` | 保持通过（现有导入名全部保留） | 不修改 |
| `README.md` | 管理页面与别名接入说明 | 修改 |

---

### 任务 1：SQL 三级判定模块

**文件：**
- 创建：`src/mysql_mcp_server/sql_classify.py`
- 测试：`tests/test_classify.py`

- [ ] **步骤 1.1：编写失败的测试**

创建 `tests/test_classify.py`：

```python
import pytest
from mysql_mcp_server.sql_classify import classify, strip_comments, first_keyword


class TestStripComments:
    def test_line_comment(self):
        assert strip_comments("-- hi\nSELECT 1") == "  \nSELECT 1"

    def test_hash_comment(self):
        assert strip_comments("# hi\nSELECT 1") == "  \nSELECT 1"

    def test_block_comment(self):
        assert strip_comments("/* hi */ SELECT 1") == "   SELECT 1"

    def test_block_comment_multiline(self):
        assert strip_comments("/* a\nb */ SELECT 1") == "   SELECT 1"


class TestFirstKeyword:
    def test_select(self):
        assert first_keyword("select * from t") == "SELECT"

    def test_leading_paren(self):
        assert first_keyword("(SELECT 1)") == "SELECT"

    def test_empty(self):
        assert first_keyword("") == ""
        assert first_keyword("   ") == ""


class TestClassify:
    @pytest.mark.parametrize("sql", [
        "SELECT * FROM t",
        "select 1",
        "SHOW TABLES",
        "DESCRIBE t",
        "DESC t",
        "EXPLAIN SELECT 1",
        "-- note\nSELECT 1",
        "/* note */ SELECT 1",
        "(SELECT 1)",
        "SELECT * FROM t FOR UPDATE",
    ])
    def test_read(self, sql):
        assert classify(sql) == "read"

    @pytest.mark.parametrize("sql", [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x = 1",
        "REPLACE INTO t VALUES (1)",
        "CREATE TABLE t (id INT)",
        "ALTER TABLE t DROP COLUMN c",
        "RENAME TABLE a TO b",
        "SET @x = 1",
        "GRANT ALL ON *.* TO u",
        "CALL p()",
        "LOCK TABLES t WRITE",
        "ANALYZE TABLE t",
        "OPTIMIZE TABLE t",
        "  update t set x=1",
    ])
    def test_write(self, sql):
        assert classify(sql) == "write"

    @pytest.mark.parametrize("sql", [
        "DELETE FROM t",
        "TRUNCATE TABLE t",
        "DROP TABLE t",
        "DROP DATABASE db",
        "drop table t",
    ])
    def test_delete(self, sql):
        assert classify(sql) == "delete"

    @pytest.mark.parametrize("sql", [
        "WITH c AS (SELECT 1) SELECT * FROM c",
        "with cte as (select 1) select * from cte",
    ])
    def test_cte_read(self, sql):
        assert classify(sql) == "read"

    @pytest.mark.parametrize("sql", [
        "WITH c AS (SELECT 1) INSERT INTO t SELECT * FROM c",
        "WITH c AS (SELECT 1) UPDATE t JOIN c ON t.id = c.id SET t.x = 1",
        "WITH c AS (SELECT 1) REPLACE INTO t SELECT * FROM c",
    ])
    def test_cte_write(self, sql):
        assert classify(sql) == "write"

    @pytest.mark.parametrize("sql", [
        "WITH c AS (SELECT 1) DELETE FROM t",
        "WITH c AS (SELECT 1) DELETE t FROM t JOIN c ON t.id = c.id",
    ])
    def test_cte_delete(self, sql):
        assert classify(sql) == "delete"

    def test_cte_nested_parens(self):
        # CTE 内嵌套括号中的 SELECT 不算主语句
        assert classify("WITH c AS (SELECT * FROM (SELECT 1) x) DELETE FROM t") == "delete"

    def test_cte_identifier_not_keyword(self):
        # 标识符 total_select 不会被误判为 SELECT 关键字
        assert classify("WITH c AS (SELECT 1) UPDATE t SET total_select = 2") == "write"

    @pytest.mark.parametrize("sql", ["", "   ", "FOO BAR", "123", "WITH c AS (SELECT 1) MERGE INTO t"])
    def test_unknown_defaults_to_write(self, sql):
        assert classify(sql) == "write"
```

- [ ] **步骤 1.2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_classify.py -v`
预期：收集错误 `ModuleNotFoundError: No module named 'mysql_mcp_server.sql_classify'`

- [ ] **步骤 1.3：编写实现**

创建 `src/mysql_mcp_server/sql_classify.py`：

```python
"""SQL 语句三级判定：read / write / delete。

规则（见设计文档 4.1-4.2 节）：
- 去掉注释后取首个关键字判定
- WITH (CTE) 语句取括号深度 0 处最后出现的主语句关键字
- 无法判定的一律按 write 处理（安全兜底）
"""

import re

READ_PREFIXES = {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
WRITE_PREFIXES = {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP",
                  "TRUNCATE", "RENAME", "GRANT", "REVOKE", "SET", "LOCK", "UNLOCK",
                  "CALL", "LOAD", "OPTIMIZE", "ANALYZE", "REPAIR", "FLUSH", "RESET", "KILL"}
DELETE_PREFIXES = {"DELETE", "TRUNCATE", "DROP"}

_CTE_MAIN_KEYWORDS = {"SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE"}
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*|\(|\)|.")


def strip_comments(sql: str) -> str:
    """去掉 -- 行注释、# 行注释、块注释。不处理字符串字面量内的注释符号（可接受，判定只取关键字）。"""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"#[^\n]*", " ", sql)
    return sql


def first_keyword(sql: str) -> str:
    """返回去掉注释与左括号后的首个关键字（大写）；无则空串。"""
    text = strip_comments(sql).strip().lstrip("(").strip()
    m = re.match(r"[A-Za-z]+", text)
    return m.group(0).upper() if m else ""


def _top_level_tokens(text: str):
    """产出括号深度 0 处的 token（标识符整词匹配，避免误配列名子串）。"""
    depth = 0
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            yield tok


def cte_main_keyword(sql: str) -> str:
    """WITH (CTE) 语句：返回顶层最后出现的主语句关键字（大写）；无则空串。"""
    last = ""
    for tok in _top_level_tokens(strip_comments(sql)):
        if tok.upper() in _CTE_MAIN_KEYWORDS:
            last = tok.upper()
    return last


def classify(sql: str) -> str:
    """判定 SQL 语句类型：'read' | 'write' | 'delete'。默认 'write'（安全兜底）。"""
    kw = first_keyword(sql)
    if kw == "WITH":
        main = cte_main_keyword(sql)
        if main in DELETE_PREFIXES:
            return "delete"
        if main in READ_PREFIXES:
            return "read"
        return "write"
    if kw in DELETE_PREFIXES:
        return "delete"
    if kw in READ_PREFIXES:
        return "read"
    return "write"
```

- [ ] **步骤 1.4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_classify.py -v`
预期：全部 PASS

- [ ] **步骤 1.5：Commit**

```
git add src/mysql_mcp_server/sql_classify.py tests/test_classify.py
git commit -m "feat: add SQL read/write/delete classifier with CTE support"
```

---

### 任务 2：配置管理模块

**文件：**
- 创建：`src/mysql_mcp_server/db_config.py`
- 测试：`tests/test_db_config.py`

- [ ] **步骤 2.1：编写失败的测试**

创建 `tests/test_db_config.py`：

```python
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
```

- [ ] **步骤 2.2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_db_config.py -v`
预期：收集错误 `No module named 'mysql_mcp_server.db_config'`

- [ ] **步骤 2.3：编写实现**

创建 `src/mysql_mcp_server/db_config.py`：

```python
"""databases.json 配置管理：加载（内存缓存）、校验、原子保存、别名解析、环境变量向后兼容、密码脱敏。"""

import json
import os
import re
import tempfile
from pathlib import Path
from threading import RLock

# 配置目录可用环境变量覆盖；默认为进程工作目录下的 config/
CONFIG_DIR = Path(os.getenv("MYSQL_MCP_CONFIG_DIR", str(Path.cwd() / "config")))
CONFIG_FILE = CONFIG_DIR / "databases.json"

_lock = RLock()
_cache: dict | None = None

_ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_VALID_POLICIES = {"client_confirm", "elicitation_only"}


def _empty_config() -> dict:
    return {"default_alias": None, "databases": {}}


def _env_entry() -> dict | None:
    """环境变量存在时构造向后兼容的单库配置（读/写同账号）。"""
    user = os.getenv("MYSQL_USER")
    password = os.getenv("MYSQL_PASSWORD")
    if not user or password is None:
        return None
    return {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "database": os.getenv("MYSQL_DATABASE"),
        "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
        "read_user": {"user": user, "password": password},
        "write_user": {"user": user, "password": password},
        "write_policy": "client_confirm",
        "allow_delete": os.getenv("MYSQL_ALLOW_DELETE", "false").lower() == "true",
    }


def load_config() -> dict:
    """读取配置（带内存缓存）。JSON 无任何数据库时回退环境变量条目。"""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        if CONFIG_FILE.exists():
            try:
                _cache = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                _cache = _empty_config()
        else:
            _cache = _empty_config()
        if not _cache.get("databases"):
            env = _env_entry()
            if env:
                _cache = {"default_alias": "default", "databases": {"default": env}}
        return _cache


def save_config(cfg: dict) -> None:
    """原子保存：先写临时文件再 os.replace，失败时清理临时文件。"""
    global _cache
    with _lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(CONFIG_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_FILE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _cache = cfg


def reset_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def validate_entry(alias: str, entry: dict) -> None:
    """校验别名与条目必填项，非法时抛 ValueError（中文消息供管理页面展示）。"""
    if not alias or not _ALIAS_RE.match(alias):
        raise ValueError("别名只能包含字母、数字、下划线、连字符，长度 1-64")
    if not entry.get("host"):
        raise ValueError("host 不能为空")
    try:
        port = int(entry.get("port", 3306))
    except (TypeError, ValueError):
        raise ValueError("port 必须是整数")
    if not (1 <= port <= 65535):
        raise ValueError("port 必须在 1-65535 之间")
    for role in ("read_user", "write_user"):
        acct = entry.get(role) or {}
        if not acct.get("user"):
            raise ValueError(f"{role}.user 不能为空")
    if entry.get("write_policy", "client_confirm") not in _VALID_POLICIES:
        raise ValueError("write_policy 只能是 client_confirm 或 elicitation_only")


def normalize_entry(entry: dict) -> dict:
    """只保留已知字段并填默认值（未知字段丢弃）。"""
    def _acct(role):
        acct = entry.get(role) or {}
        return {"user": acct.get("user", ""), "password": acct.get("password", "")}

    ssh = entry.get("ssh") or {}
    return {
        "host": entry.get("host", "localhost"),
        "port": int(entry.get("port", 3306)),
        "database": entry.get("database") or None,
        "charset": entry.get("charset") or "utf8mb4",
        "sql_mode": entry.get("sql_mode") or "TRADITIONAL",
        "connect_timeout": int(entry.get("connect_timeout", 10)),
        "read_user": _acct("read_user"),
        "write_user": _acct("write_user"),
        "write_policy": entry.get("write_policy", "client_confirm"),
        "allow_delete": bool(entry.get("allow_delete", False)),
        "ssh": {
            "enable": bool(ssh.get("enable", False)),
            "host": ssh.get("host"),
            "port": int(ssh.get("port", 22)),
            "user": ssh.get("user"),
            "key_path": ssh.get("key_path"),
            "remote_host": ssh.get("remote_host", "localhost"),
            "remote_port": int(ssh.get("remote_port", 3306)),
            "local_port": int(ssh.get("local_port", 3330)),
        },
    }


def resolve(alias: str | None) -> tuple[str, dict] | None:
    """解析别名到 (alias, entry)。alias=None 时用 default_alias；解析失败返回 None。"""
    cfg = load_config()
    dbs = cfg.get("databases", {})
    if alias:
        return (alias, dbs[alias]) if alias in dbs else None
    default = cfg.get("default_alias")
    if default and default in dbs:
        return default, dbs[default]
    return None


def masked_config(cfg: dict) -> dict:
    """深拷贝并把非空密码替换为 '****'（原配置不动）。"""
    out = json.loads(json.dumps(cfg))
    for e in out.get("databases", {}).values():
        for role in ("read_user", "write_user"):
            acct = e.get(role)
            if isinstance(acct, dict) and acct.get("password"):
                acct["password"] = "****"
    return out
```

- [ ] **步骤 2.4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_db_config.py -v`
预期：全部 PASS

- [ ] **步骤 2.5：Commit**

```
git add src/mysql_mcp_server/db_config.py tests/test_db_config.py
git commit -m "feat: add databases.json config store with alias resolution and env fallback"
```

---

### 任务 3：审计模块

**文件：**
- 创建：`src/mysql_mcp_server/audit.py`
- 测试：`tests/test_audit.py`

- [ ] **步骤 3.1：编写失败的测试**

创建 `tests/test_audit.py`：

```python
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


def test_cap_100():
    for i in range(150):
        audit.record("db1", "UPDATE", "client", f"UPDATE t SET x={i}")
    assert len(audit.list_entries()) == 100
    assert audit.list_entries()[-1]["sql"] == "UPDATE t SET x=149"


def test_sql_truncated_to_200():
    audit.record("db1", "UPDATE", "client", "x" * 500)
    assert len(audit.list_entries()[0]["sql"]) == 200


def test_file_written():
    audit.record("db1", "DELETE", "elicitation", "DELETE FROM t")
    log_file = audit.LOG_DIR / "audit.log"
    assert log_file.exists()
    assert "DELETE FROM t" in log_file.read_text(encoding="utf-8")
```

- [ ] **步骤 3.2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_audit.py -v`
预期：收集错误 `No module named 'mysql_mcp_server.audit'`

- [ ] **步骤 3.3：编写实现**

创建 `src/mysql_mcp_server/audit.py`：

```python
"""写操作审计：内存最近 100 条 + 追加文件日志（文件失败不影响主流程）。"""

from collections import deque
from datetime import datetime
from pathlib import Path
from threading import Lock

LOG_DIR = Path("logs")

_entries: deque = deque(maxlen=100)
_lock = Lock()


def record(alias: str, sql_type: str, channel: str, sql: str, status: str = "executed") -> dict:
    """记录一条写操作审计并返回该条目。"""
    entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "alias": alias,
        "type": sql_type,
        "channel": channel,
        "status": status,
        "sql": sql[:200],
    }
    with _lock:
        _entries.append(entry)
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_DIR / "audit.log", "a", encoding="utf-8") as f:
                f.write(f"{entry['time']} | {entry['alias']} | {entry['type']} | "
                        f"{entry['channel']} | {entry['status']} | {entry['sql']}\n")
        except OSError:
            pass
    return entry


def list_entries() -> list[dict]:
    with _lock:
        return list(_entries)


def clear() -> None:
    with _lock:
        _entries.clear()
```

- [ ] **步骤 3.4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_audit.py -v`
预期：全部 PASS

- [ ] **步骤 3.5：Commit**

```
git add src/mysql_mcp_server/audit.py tests/test_audit.py
git commit -m "feat: add write-operation audit log (memory ring + file append)"
```

---

### 任务 4：管理 API

**文件：**
- 创建：`src/mysql_mcp_server/admin_api.py`
- 测试：`tests/test_admin_api.py`

- [ ] **步骤 4.1：编写失败的测试**

创建 `tests/test_admin_api.py`：

```python
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
```

- [ ] **步骤 4.2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_admin_api.py -v`
预期：收集错误 `No module named 'mysql_mcp_server.admin_api'`

- [ ] **步骤 4.3：编写实现**

创建 `src/mysql_mcp_server/admin_api.py`：

```python
"""管理页面 REST API：配置 CRUD、连接测试、默认别名设置、审计、健康检查。

仅允许回环地址访问；配置变更通过回调通知（server 模块注册以失效别名实例缓存）。
"""

from pathlib import Path

import anyio
import mysql.connector
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from mysql_mcp_server import audit, db_config

STATIC_DIR = Path(__file__).parent / "static"

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

_on_change_callbacks = []


def register_on_change(cb) -> None:
    """注册配置变更回调 cb(alias: str)。"""
    _on_change_callbacks.append(cb)


def _notify_changed(alias: str) -> None:
    for cb in _on_change_callbacks:
        try:
            cb(alias)
        except Exception:
            pass


def _guard(request: Request) -> JSONResponse | None:
    ip = request.client.host if request.client else ""
    if ip not in _LOOPBACK:
        return JSONResponse({"error": "forbidden: admin API is loopback-only"}, status_code=403)
    return None


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def get_databases(request: Request):
    if (g := _guard(request)):
        return g
    return JSONResponse(db_config.masked_config(db_config.load_config()))


async def create_database(request: Request):
    if (g := _guard(request)):
        return g
    body = await _body(request)
    alias = str(body.get("alias", "")).strip()
    try:
        db_config.validate_entry(alias, body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    cfg = db_config.load_config()
    if alias in cfg["databases"]:
        return JSONResponse({"error": f"别名 {alias} 已存在"}, status_code=409)
    cfg["databases"][alias] = db_config.normalize_entry(body)
    if not cfg.get("default_alias"):
        cfg["default_alias"] = alias
    db_config.save_config(cfg)
    _notify_changed(alias)
    return JSONResponse({"ok": True, "default_alias": cfg["default_alias"]}, status_code=201)


async def update_database(request: Request):
    if (g := _guard(request)):
        return g
    alias = request.path_params["alias"]
    body = await _body(request)
    cfg = db_config.load_config()
    old = cfg["databases"].get(alias)
    if old is None:
        return JSONResponse({"error": f"别名 {alias} 不存在"}, status_code=404)
    try:
        db_config.validate_entry(alias, body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    new = db_config.normalize_entry(body)
    # 密码为空表示不修改
    for role in ("read_user", "write_user"):
        if not new[role]["password"]:
            new[role]["password"] = old[role]["password"]
    cfg["databases"][alias] = new
    db_config.save_config(cfg)
    _notify_changed(alias)
    return JSONResponse({"ok": True})


async def delete_database(request: Request):
    if (g := _guard(request)):
        return g
    alias = request.path_params["alias"]
    cfg = db_config.load_config()
    if alias not in cfg["databases"]:
        return JSONResponse({"error": f"别名 {alias} 不存在"}, status_code=404)
    del cfg["databases"][alias]
    if cfg.get("default_alias") == alias:
        remaining = sorted(cfg["databases"])
        cfg["default_alias"] = remaining[0] if remaining else None
    db_config.save_config(cfg)
    _notify_changed(alias)
    return JSONResponse({"ok": True, "default_alias": cfg["default_alias"]})


async def test_connection(request: Request):
    if (g := _guard(request)):
        return g
    alias = request.path_params["alias"]
    body = await _body(request)
    cfg = db_config.load_config()
    entry = cfg["databases"].get(alias)
    if entry is None:
        return JSONResponse({"error": f"别名 {alias} 不存在"}, status_code=404)
    role = "write" if body.get("as_write") else "read"
    acct = entry[f"{role}_user"]
    conn_cfg = {
        "host": entry["host"],
        "port": int(entry.get("port", 3306)),
        "user": acct["user"],
        "password": acct["password"],
        "connect_timeout": 5,
    }

    def _try() -> str | None:
        try:
            conn = mysql.connector.connect(**conn_cfg)
            conn.close()
            return None
        except mysql.connector.Error as e:
            return str(getattr(e, "msg", None) or e)

    error = await anyio.to_thread.run_sync(_try)
    if error is None:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": error, "role": role})


async def update_settings(request: Request):
    if (g := _guard(request)):
        return g
    body = await _body(request)
    alias = str(body.get("default_alias", "")).strip()
    cfg = db_config.load_config()
    if alias not in cfg["databases"]:
        return JSONResponse({"error": f"别名 {alias} 不存在"}, status_code=400)
    cfg["default_alias"] = alias
    db_config.save_config(cfg)
    return JSONResponse({"ok": True, "default_alias": alias})


async def get_audit(request: Request):
    if (g := _guard(request)):
        return g
    return JSONResponse(audit.list_entries())


async def get_health(request: Request):
    if (g := _guard(request)):
        return g
    cfg = db_config.load_config()
    return JSONResponse({"status": "ok", "databases": len(cfg.get("databases", {}))})


def create_admin_app() -> Starlette:
    """独立的 admin Starlette 子应用（API + 静态页），由主服务挂载或测试直接使用。"""
    return Starlette(routes=[
        Route("/api/databases", get_databases, methods=["GET"]),
        Route("/api/databases", create_database, methods=["POST"]),
        Route("/api/databases/{alias}", update_database, methods=["PUT"]),
        Route("/api/databases/{alias}", delete_database, methods=["DELETE"]),
        Route("/api/databases/{alias}/test", test_connection, methods=["POST"]),
        Route("/api/settings", update_settings, methods=["PUT"]),
        Route("/api/audit", get_audit, methods=["GET"]),
        Route("/api/health", get_health, methods=["GET"]),
        Mount("/admin", app=StaticFiles(directory=STATIC_DIR, html=True, check_dir=False), name="admin"),
    ])
```

注意：静态目录 `static/` 在任务 5 才创建，`check_dir=False` 保证此时代码可导入、API 测试可跑。

- [ ] **步骤 4.4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_admin_api.py -v`
预期：全部 PASS

- [ ] **步骤 4.5：Commit**

```
git add src/mysql_mcp_server/admin_api.py tests/test_admin_api.py
git commit -m "feat: add loopback-guarded admin REST API for database config CRUD"
```

---

### 任务 5：管理页面前端

**文件：**
- 创建：`src/mysql_mcp_server/static/index.html`
- 测试：追加到 `tests/test_admin_api.py`

- [ ] **步骤 5.1：先写页面可访问的失败测试**

在 `tests/test_admin_api.py` 末尾追加：

```python
class TestAdminPage:
    def test_admin_index_served(self, client, no_env):
        r = client.get("/admin/")
        assert r.status_code == 200
        assert "MySQL MCP 管理页面" in r.text
```

- [ ] **步骤 5.2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_admin_api.py::TestAdminPage -v`
预期：FAIL（404，static 目录不存在）

- [ ] **步骤 5.3：编写前端页面**

创建 `src/mysql_mcp_server/static/index.html`（完整单文件，内联 CSS/JS）：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MySQL MCP 管理页面</title>
<style>
  :root { --line:#e2e8f0; --muted:#64748b; --blue:#2563eb; --red:#dc2626; --green:#16a34a; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:system-ui,-apple-system,"Segoe UI",sans-serif; background:#f1f5f9; color:#0f172a; padding:24px; }
  h1 { font-size:20px; margin-bottom:16px; display:flex; align-items:center; gap:10px; }
  .dot { width:10px; height:10px; border-radius:50%; background:var(--green); display:inline-block; }
  .card { background:#fff; border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:16px; }
  .card h2 { font-size:15px; margin-bottom:12px; }
  .db-item { border:1px solid var(--line); border-radius:8px; padding:12px; margin-bottom:10px; }
  .db-head { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .db-head .name { font-weight:600; font-size:15px; }
  .badge { font-size:12px; padding:2px 8px; border-radius:99px; background:#eff6ff; color:var(--blue); }
  .badge.warn { background:#fef2f2; color:var(--red); font-weight:600; }
  .db-meta { color:var(--muted); font-size:13px; margin-top:6px; line-height:1.7; }
  .actions { margin-top:8px; display:flex; gap:8px; }
  button { cursor:pointer; border:1px solid var(--line); background:#fff; border-radius:6px; padding:5px 12px; font-size:13px; }
  button:hover { background:#f8fafc; }
  button.primary { background:var(--blue); border-color:var(--blue); color:#fff; }
  button.danger { color:var(--red); border-color:#fecaca; }
  .add-bar { display:flex; justify-content:flex-end; margin-bottom:10px; }
  .sse-url { font-family:ui-monospace,Consolas,monospace; font-size:13px; background:#f8fafc; padding:2px 6px; border-radius:4px; cursor:pointer; }
  .sse-url:hover { background:#e2e8f0; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:500; }
  .sql-cell { font-family:ui-monospace,Consolas,monospace; max-width:420px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .modal-mask { position:fixed; inset:0; background:rgba(15,23,42,.45); display:none; align-items:flex-start; justify-content:center; padding:40px 16px; overflow:auto; z-index:10; }
  .modal-mask.open { display:flex; }
  .modal { background:#fff; border-radius:10px; padding:20px; width:560px; max-width:100%; }
  .modal h3 { font-size:16px; margin-bottom:14px; }
  fieldset { border:1px solid var(--line); border-radius:8px; padding:10px 12px; margin-bottom:12px; }
  legend { font-size:13px; color:var(--muted); padding:0 6px; }
  .form-row { display:flex; gap:12px; margin-bottom:8px; flex-wrap:wrap; }
  .form-row label { flex:1; min-width:120px; font-size:13px; display:flex; flex-direction:column; gap:4px; }
  input, select { border:1px solid var(--line); border-radius:6px; padding:6px 8px; font-size:13px; width:100%; }
  .checkbox-row { display:flex; align-items:center; gap:8px; font-size:13px; margin:6px 0; }
  .checkbox-row input { width:auto; }
  .modal-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:14px; }
  .msg { font-size:13px; margin-top:8px; min-height:18px; }
  .msg.ok { color:var(--green); } .msg.err { color:var(--red); }
  details { margin-bottom:8px; } details summary { cursor:pointer; font-size:13px; color:var(--muted); }
  #toggle-default { font-size:13px; color:var(--muted); }
</style>
</head>
<body>
<h1><span class="dot"></span> MySQL MCP 管理页面</h1>

<div class="card">
  <div class="add-bar"><button class="primary" onclick="openModal()">+ 添加数据库</button></div>
  <h2>数据库列表</h2>
  <div id="db-list"><p style="color:var(--muted)">加载中…</p></div>
</div>

<div class="card">
  <h2>客户端接入说明</h2>
  <div id="sse-help"><p style="color:var(--muted)">添加数据库后此处显示各别名的 SSE URL（点击复制）。</p></div>
</div>

<div class="card">
  <h2>写操作审计（最近 100 条） <button onclick="loadAudit()">刷新</button></h2>
  <table>
    <thead><tr><th>时间</th><th>别名</th><th>类型</th><th>确认通道</th><th>状态</th><th>SQL</th></tr></thead>
    <tbody id="audit-body"></tbody>
  </table>
</div>

<div class="modal-mask" id="modal-mask">
  <div class="modal">
    <h3 id="modal-title">添加数据库</h3>
    <div class="msg" id="form-msg"></div>
    <form id="db-form" onsubmit="return saveDb(event)">
      <fieldset>
        <legend>基本信息</legend>
        <div class="form-row">
          <label>别名*<input name="alias" required pattern="[A-Za-z0-9_-]{1,64}" placeholder="如 db1"></label>
          <label>主机<input name="host" required value="localhost"></label>
          <label>端口<input name="port" type="number" value="3306"></label>
        </div>
        <div class="form-row">
          <label>数据库名（留空=多库模式）<input name="database" placeholder="mydb"></label>
          <label>字符集<input name="charset" value="utf8mb4"></label>
        </div>
      </fieldset>
      <fieldset>
        <legend>查询用户（SELECT/SHOW 等读操作）</legend>
        <div class="form-row">
          <label>用户名*<input name="read_user" required></label>
          <label>密码<input name="read_password" type="password" placeholder="编辑时留空=不修改"></label>
        </div>
      </fieldset>
      <fieldset>
        <legend>操作用户（确认后的写/删除操作）</legend>
        <div class="form-row">
          <label>用户名*<input name="write_user" required></label>
          <label>密码<input name="write_password" type="password" placeholder="编辑时留空=不修改"></label>
        </div>
      </fieldset>
      <fieldset>
        <legend>权限策略</legend>
        <div class="form-row">
          <label>写确认策略
            <select name="write_policy">
              <option value="client_confirm">client_confirm（客户端确认，默认）</option>
              <option value="elicitation_only">elicitation_only（仅服务端确认）</option>
            </select>
          </label>
        </div>
        <div class="checkbox-row">
          <input type="checkbox" name="allow_delete" id="allow-delete">
          <label for="allow-delete">允许删除操作（DELETE / TRUNCATE / DROP，开启前请谨慎评估）</label>
        </div>
      </fieldset>
      <details>
        <summary>高级（SSH 隧道等）</summary>
        <div class="checkbox-row">
          <input type="checkbox" name="ssh_enable" id="ssh-enable">
          <label for="ssh-enable">启用 SSH 隧道</label>
        </div>
        <div class="form-row">
          <label>SSH 主机<input name="ssh_host"></label>
          <label>SSH 端口<input name="ssh_port" type="number" value="22"></label>
          <label>SSH 用户<input name="ssh_user"></label>
        </div>
        <div class="form-row">
          <label>密钥路径<input name="ssh_key_path"></label>
          <label>本地端口<input name="ssh_local_port" type="number" value="3330"></label>
        </div>
      </details>
      <div class="modal-actions">
        <button type="button" class="primary" onclick="testConn()">测试连接（读账号）</button>
        <button type="button" onclick="closeModal()">取消</button>
        <button type="submit" class="primary">保存</button>
      </div>
    </form>
  </div>
</div>

<script>
let editingAlias = null;

async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body;
}

async function loadDbs() {
  const cfg = await api('/api/databases');
  const list = document.getElementById('db-list');
  const help = document.getElementById('sse-help');
  const aliases = Object.keys(cfg.databases);
  if (!aliases.length) {
    list.innerHTML = '<p style="color:var(--muted)">暂无配置，点击右上角添加。</p>';
    help.innerHTML = '<p style="color:var(--muted)">添加数据库后此处显示 SSE URL。</p>';
    return;
  }
  list.innerHTML = aliases.map(a => {
    const e = cfg.databases[a];
    const isDefault = cfg.default_alias === a;
    const del = e.allow_delete
      ? '<span class="badge warn">删除权限: 开 ⚠</span>' : '';
    return `<div class="db-item">
      <div class="db-head">
        <span class="name">${a}</span>
        ${isDefault ? '<span class="badge">默认</span>' : `<button id="toggle-default" onclick="setDefault('${a}')">设为默认</button>`}
        ${del}
      </div>
      <div class="db-meta">
        ${e.host}:${e.port}${e.database ? '/' + e.database : '（多库模式）'}<br>
        读: ${e.read_user.user} ｜ 写: ${e.write_user.user}<br>
        写策略: ${e.write_policy}
      </div>
      <div class="actions">
        <button onclick="openModal('${a}')">编辑</button>
        <button onclick="testExisting('${a}', false)">测试连接</button>
        <button onclick="testExisting('${a}', true)">测试写账号</button>
        <button class="danger" onclick="removeDb('${a}')">删除</button>
      </div>
    </div>`;
  }).join('');
  help.innerHTML = aliases.map(a =>
    `<div>${a} → <span class="sse-url" onclick="copySse('${a}')" title="点击复制">http://127.0.0.1:8000/sse?alias=${a}</span></div>`
  ).join('') +
  '<p style="color:var(--muted);font-size:12px;margin-top:6px">TRAE/Claude 配置：在 MCP 服务器设置中选择 SSE 类型，URL 填上面的地址。</p>';
}

async function loadAudit() {
  const rows = await api('/api/audit');
  document.getElementById('audit-body').innerHTML = rows.slice().reverse().map(e =>
    `<tr><td>${e.time}</td><td>${e.alias}</td><td>${e.type}</td><td>${e.channel}</td><td>${e.status}</td><td class="sql-cell" title="${e.sql.replace(/"/g,'&quot;')}">${e.sql}</td></tr>`
  ).join('') || '<tr><td colspan="6" style="color:var(--muted)">暂无记录</td></tr>';
}

function openModal(alias) {
  editingAlias = alias || null;
  document.getElementById('modal-title').textContent = alias ? `编辑 ${alias}` : '添加数据库';
  document.getElementById('form-msg').textContent = '';
  const f = document.getElementById('db-form');
  f.reset();
  if (alias) {
    api('/api/databases').then(cfg => {
      const e = cfg.databases[alias];
      if (!e) return;
      f.alias.value = alias; f.alias.readOnly = true;
      f.host.value = e.host; f.port.value = e.port;
      f.database.value = e.database || ''; f.charset.value = e.charset || 'utf8mb4';
      f.read_user.value = e.read_user.user; f.write_user.value = e.write_user.user;
      f.write_policy.value = e.write_policy;
      f.allow_delete.checked = !!e.allow_delete;
      if (e.ssh && e.ssh.enable) {
        f.ssh_enable.checked = true;
        f.ssh_host.value = e.ssh.host || ''; f.ssh_port.value = e.ssh.port || 22;
        f.ssh_user.value = e.ssh.user || ''; f.ssh_key_path.value = e.ssh.key_path || '';
        f.ssh_local_port.value = e.ssh.local_port || 3330;
      }
    });
  } else {
    f.alias.readOnly = false;
  }
  document.getElementById('modal-mask').classList.add('open');
}

function closeModal() { document.getElementById('modal-mask').classList.remove('open'); }

function collectForm(masked) {
  const f = document.getElementById('db-form');
  return {
    alias: f.alias.value.trim(), host: f.host.value.trim(), port: +f.port.value,
    database: f.database.value.trim(), charset: f.charset.value.trim(),
    read_user: { user: f.read_user.value.trim(), password: f.read_password.value },
    write_user: { user: f.write_user.value.trim(), password: f.write_password.value },
    write_policy: f.write_policy.value, allow_delete: f.allow_delete.checked,
    ssh: {
      enable: f.ssh_enable.checked, host: f.ssh_host.value.trim(), port: +f.ssh_port.value,
      user: f.ssh_user.value.trim(), key_path: f.ssh_key_path.value.trim(),
      remote_host: 'localhost', remote_port: 3306, local_port: +f.ssh_local_port.value,
    },
  };
}

async function saveDb(ev) {
  ev.preventDefault();
  const msg = document.getElementById('form-msg');
  msg.className = 'msg';
  try {
    const body = collectForm();
    if (editingAlias) {
      await api(`/api/databases/${encodeURIComponent(editingAlias)}`, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    } else {
      await api('/api/databases', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    }
    closeModal(); await loadDbs();
  } catch (e) { msg.className = 'msg err'; msg.textContent = e.message; }
  return false;
}

async function testConn() {
  const msg = document.getElementById('form-msg');
  msg.className = 'msg'; msg.textContent = '测试中…';
  try {
    const body = collectForm();
    await api('/api/databases', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const r = await api(`/api/databases/${encodeURIComponent(body.alias)}/test`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
    if (!r.ok) throw new Error(r.error);
    msg.className = 'msg ok'; msg.textContent = '连接成功';
    await loadDbs();
  } catch (e) { msg.className = 'msg err'; msg.textContent = '连接失败：' + e.message; }
}

async function testExisting(alias, asWrite) {
  try {
    const r = await api(`/api/databases/${encodeURIComponent(alias)}/test`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ as_write: asWrite }) });
    alert(r.ok ? '连接成功' : '连接失败：' + r.error);
  } catch (e) { alert('连接失败：' + e.message); }
}

async function removeDb(alias) {
  if (!confirm(`确定删除配置 ${alias}？（不会影响数据库本身）`)) return;
  await api(`/api/databases/${encodeURIComponent(alias)}`, { method: 'DELETE' });
  await loadDbs();
}

async function setDefault(alias) {
  await api('/api/settings', { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ default_alias: alias }) });
  await loadDbs();
}

function copySse(alias) {
  const url = `http://127.0.0.1:8000/sse?alias=${alias}`;
  navigator.clipboard.writeText(url).then(() => alert('已复制：' + url));
}

loadDbs(); loadAudit();
</script>
</body>
</html>
```

- [ ] **步骤 5.4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_admin_api.py -v`
预期：全部 PASS（含 TestAdminPage）

- [ ] **步骤 5.5：Commit**

```
git add src/mysql_mcp_server/static/index.html tests/test_admin_api.py
git commit -m "feat: add single-file admin UI for database config management"
```

---

### 任务 6：server.py 核心——双账号执行与确认流程

改造 [server.py](file:///e:/coding/mysql/mysql_mcp_server/src/mysql_mcp_server/server.py)。现有模块级名字（`app`、`list_tools`、`call_tool`、`get_db_config` 等）**全部保留**，`tests/test_server.py` 不修改且必须保持通过。

**文件：**
- 修改：`src/mysql_mcp_server/server.py`
- 测试：创建 `tests/test_execute_policy.py`

- [ ] **步骤 6.1：编写失败的测试**

创建 `tests/test_execute_policy.py`：

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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
    with patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[__import__("mcp.types", fromlist=["TextContent"]).TextContent(type="text", text="ok")])) as rq:
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
    ok = __import__("mcp.types", fromlist=["TextContent"]).TextContent(type="text", text="done")
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
async def test_write_fallback_client_confirm():
    ok = __import__("mcp.types", fromlist=["TextContent"]).TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])) as rq:
        result = await execute_sql_entry("db1", make_entry(write_policy="client_confirm"), "INSERT INTO t VALUES (1)")
    assert text_of(result) == "done"
    assert rq.await_args.args[2] == "write"


@pytest.mark.asyncio
async def test_write_fallback_elicitation_only_rejected():
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock()) as rq:
        result = await execute_sql_entry("db1", make_entry(write_policy="elicitation_only"), "INSERT INTO t VALUES (1)")
    assert "elicitation_only" in text_of(result)
    rq.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_allowed_with_confirm():
    ok = __import__("mcp.types", fromlist=["TextContent"]).TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value="accept")), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])) as rq:
        result = await execute_sql_entry("db1", make_entry(allow_delete=True), "DROP TABLE t")
    assert text_of(result) == "done"
    assert rq.await_args.args[2] == "write"


@pytest.mark.asyncio
async def test_audit_recorded_on_client_fallback(tmp_path, monkeypatch):
    from mysql_mcp_server import audit as audit_mod
    monkeypatch.setattr(audit_mod, "LOG_DIR", tmp_path)
    audit_mod.clear()
    ok = __import__("mcp.types", fromlist=["TextContent"]).TextContent(type="text", text="done")
    with patch("mysql_mcp_server.server.elicit_confirm", new=AsyncMock(return_value=None)), \
         patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])):
        await execute_sql_entry("db1", make_entry(), "UPDATE t SET x=1")
    entries = audit_mod.list_entries()
    assert entries[-1]["channel"] == "client"
    assert entries[-1]["type"] == "UPDATE"
```

- [ ] **步骤 6.2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_execute_policy.py -v`
预期：ImportError，`cannot import name 'build_connector_config'`

- [ ] **步骤 6.3：在 server.py 中新增双账号与确认流程代码**

修改 `src/mysql_mcp_server/server.py`。**在文件头部 import 区追加**（现有 import 保持不动）：

```python
from mysql_mcp_server import admin_api, audit, db_config
from mysql_mcp_server.sql_classify import classify
```

**在 `get_db_config` 函数之后**（约 line 186 附近，`app = Server(...)` 之前）追加：

```python
# ---------------------------------------------------------------------------
# 别名/双账号支持：从 databases.json 条目构造连接配置并执行
# ---------------------------------------------------------------------------

def build_connector_config(entry: dict, role: str, host=None, port=None) -> dict:
    """从别名条目构造 mysql.connector 配置；role='read'|'write' 选择对应账号。"""
    acct = entry[f"{role}_user"]
    config = {
        "host": host or entry["host"],
        "port": port or int(entry.get("port", 3306)),
        "user": acct["user"],
        "password": acct["password"],
        "charset": entry.get("charset") or "utf8mb4",
        "collation": entry.get("collation") or "utf8mb4_unicode_ci",
        "autocommit": True,
        "sql_mode": entry.get("sql_mode") or "TRADITIONAL",
        "connect_timeout": int(entry.get("connect_timeout", 10)),
    }
    if entry.get("database"):
        config["database"] = entry["database"]
    return {k: v for k, v in config.items() if v is not None}


@contextmanager
def maybe_ssh_tunnel_for(entry: dict):
    """按别名条目的 ssh 配置建立隧道；未启用时直连。"""
    ssh = entry.get("ssh") or {}
    if not ssh.get("enable"):
        yield entry["host"], int(entry.get("port", 3306))
        return

    local_port = int(ssh.get("local_port", 3330))
    logger.info(
        f"Starting SSH tunnel for alias: {ssh.get('user')}@{ssh.get('host')}:{ssh.get('port', 22)} "
        f"-> {local_port}:{ssh.get('remote_host', 'localhost')}:{ssh.get('remote_port', 3306)}"
    )
    ssh_cmd = [
        'ssh', '-i', ssh.get("key_path") or '',
        '-N', '-o', 'ExitOnForwardFailure=yes', '-o', 'BatchMode=yes',
        '-L', f"{local_port}:{ssh.get('remote_host', 'localhost')}:{ssh.get('remote_port', 3306)}",
        f"{ssh.get('user')}@{ssh.get('host')}", '-p', str(ssh.get("port", 22)),
    ]
    ssh_proc = subprocess.Popen(ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(2)
        if ssh_proc.poll() is not None:
            stderr = ssh_proc.stderr.read().decode()
            raise RuntimeError(f"SSH tunnel process exited prematurely: {stderr}")
        yield "127.0.0.1", local_port
    finally:
        try:
            ssh_proc.terminate()
            ssh_proc.wait(timeout=5)
        except Exception as e:
            logger.error(f"Error terminating SSH tunnel: {e}")


async def elicit_confirm(alias: str, kind_label: str, query: str, timeout: float = 30.0) -> str | None:
    """发起服务端 SQL 确认（MCP elicitation）。

    返回 'accept' / 'decline' / 'cancel'；客户端不支持或超时返回 None（由调用方降级）。
    """
    from mcp.server.lowlevel.server import request_ctx
    try:
        ctx = request_ctx.get()
    except LookupError:
        return None
    session = ctx.session
    params = session.client_params
    caps = params.capabilities if params else None
    if not caps or not caps.elicitation:
        return None
    try:
        with anyio.move_on_after(timeout):
            result = await session.elicit(
                message=f"确认在数据库别名 [{alias}] 上执行{kind_label}操作：\n\n{query}",
                requestedSchema={"type": "object", "properties": {}},
            )
            return result.action
    except Exception as e:
        logger.warning(f"Elicitation failed: {e}")
        return None
    return None  # 超时


_KIND_LABELS = {"write": "写", "delete": "删除类"}


async def execute_sql_entry(alias: str, entry: dict, query: str) -> list[TextContent]:
    """三级判定 + 双账号执行 + 双通道确认（设计文档 4.3 节）。"""
    kind = classify(query)

    if kind == "read":
        return await run_query_entry(entry, query, "read")

    if kind == "delete" and not entry.get("allow_delete"):
        audit.record(alias, "DELETE", "-", query, status="rejected_delete_disabled")
        return [TextContent(type="text", text="该别名未开启删除权限，请在管理页面开启后重试。")]

    label = _KIND_LABELS.get(kind, "写")
    action = await elicit_confirm(alias, label, query)

    if action == "accept":
        result = await run_query_entry(entry, query, "write")
        audit.record(alias, kind.upper(), "elicitation", query)
        return result
    if action in ("decline", "cancel"):
        audit.record(alias, kind.upper(), "elicitation", query, status=f"user_{action}")
        return [TextContent(type="text", text="用户已拒绝执行该 SQL。")]

    # 客户端不支持 elicitation → 按别名策略降级
    policy = entry.get("write_policy", "client_confirm")
    if policy == "elicitation_only":
        audit.record(alias, kind.upper(), "-", query, status="blocked_policy")
        return [TextContent(type="text", text=(
            "当前客户端不支持服务端确认（elicitation），"
            "且该别名策略为 elicitation_only，写操作已被拒绝。"))]
    audit.record(alias, kind.upper(), "client", query)
    return await run_query_entry(entry, query, "write")
```

**新增 `run_query_entry`**：先提取现有 `run_query` 的结果格式化为模块级纯函数（供两者共用）。在现有 `run_query` 定义之前加入：

```python
def _format_query_result(cursor, conn, query: str, config: dict) -> list[TextContent]:
    """按查询类型格式化结果（从原 run_query 提取，逻辑不变）。"""
    query_upper = query.strip().upper()

    if query_upper.startswith("SHOW TABLES"):
        tables = cursor.fetchall()
        db_name = config.get("database", "all databases")
        result = [f"Tables_in_{db_name}"]
        result.extend([table[0] for table in tables])
        return [TextContent(type="text", text="\n".join(result))]

    if any(query_upper.startswith(p) for p in ["DESCRIBE ", "DESC ", "SHOW COLUMNS FROM ", "SHOW FIELDS FROM "]):
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        results = [",".join(columns)]
        for row in rows:
            results.append(",".join(str(v) if v is not None else "NULL" for v in row))
        return [TextContent(type="text", text="\n".join(results))]

    if cursor.description is not None:
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        if not rows:
            return [TextContent(type="text", text="Query executed successfully. No results returned.")]
        result = [",".join("" if v is None else str(v) for v in row) for row in rows]
        return [TextContent(type="text", text="\n".join([",".join(columns)] + result))]

    conn.commit()
    return [TextContent(type="text", text=f"Query executed successfully. Rows affected: {cursor.rowcount}")]


async def run_query_entry(entry: dict, query: str, role: str) -> list[TextContent]:
    """用别名条目 + 指定角色账号执行 SQL。"""
    def _sync_run():
        with maybe_ssh_tunnel_for(entry) as (host, port):
            config = build_connector_config(entry, role, host, port)
            try:
                with connect(**config) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(query)
                        return _format_query_result(cursor, conn, query, config)
            except Error as e:
                error_msg = getattr(e, 'msg', None) or str(e) or 'Unknown MySQL error'
                logger.error(f"Error executing SQL: {error_msg}")
                return [TextContent(type="text", text=f"Error executing query: {error_msg}")]
    return await anyio.to_thread.run_sync(_sync_run)
```

**改造现有 `run_query`**（`_sync_run` 内部替换为共用格式化函数，行为不变）：

```python
async def run_query(query: str) -> list[TextContent]:
    """
    A helper function that handles the execution of a SQL query (env-config,
    backward compatible). Uses anyio.to_thread.run_sync to prevent blocking.
    """
    def _sync_run():
        with maybe_ssh_tunnel() as (host, port):
            config = get_db_config(host, port)
            try:
                with connect(**config) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(query)
                        return _format_query_result(cursor, conn, query, config)
            except Error as e:
                error_msg = getattr(e, 'msg', None) or str(e) or 'Unknown MySQL error'
                logger.error(f"Error executing SQL: {error_msg}")
                return [TextContent(type="text", text=f"Error executing query: {error_msg}")]
    return await anyio.to_thread.run_sync(_sync_run)
```

- [ ] **步骤 6.4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_execute_policy.py -v`
预期：全部 PASS

- [ ] **步骤 6.5：运行现有回归**

运行：`.venv\Scripts\python.exe -m pytest tests/test_server.py -v`
预期：全部 PASS（模块级名字未破坏）

- [ ] **步骤 6.6：Commit**

```
git add src/mysql_mcp_server/server.py tests/test_execute_policy.py
git commit -m "feat: dual-account SQL execution with elicitation confirm and delete gate"
```

---

### 任务 7：server.py——别名路由工具入口与 SSE 集成

**文件：**
- 修改：`src/mysql_mcp_server/server.py`
- 测试：追加到 `tests/test_execute_policy.py`

- [ ] **步骤 7.1：编写失败的测试**

在 `tests/test_execute_policy.py` 末尾追加：

```python
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
        ok = __import__("mcp.types", fromlist=["TextContent"]).TextContent(type="text", text="ok")
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
        ok = __import__("mcp.types", fromlist=["TextContent"]).TextContent(type="text", text="ok")
        with patch("mysql_mcp_server.server.run_query_entry", new=AsyncMock(return_value=[ok])) as rq:
            r = await self._call("get_table_sample", {"table_name": "t"}, alias="db1")
        assert text_of(r) == "ok"
        assert rq.await_args.args[1].startswith("SELECT * FROM")
        assert rq.await_args.args[2] == "read"


class TestCreateAliasServer:
    def test_independent_instances(self):
        from mysql_mcp_server.server import create_alias_server
        s1 = create_alias_server("db1")
        s2 = create_alias_server("db2")
        assert s1 is not s2
        assert s1.name == "mysql_mcp_server"


class TestInvalidateAlias:
    def test_clears_registry(self):
        from mysql_mcp_server import server
        server._server_registry["db1"] = object()
        server.invalidate_alias("db1")
        assert "db1" not in server._server_registry
        server._server_registry["a"] = object()
        server._server_registry["b"] = object()
        server.invalidate_alias()  # 清空全部
        assert server._server_registry == {}
```

- [ ] **步骤 7.2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_execute_policy.py -v -k "CallToolImpl or CreateAliasServer or InvalidateAlias"`
预期：ImportError（`call_tool_impl` / `create_alias_server` 不存在）

- [ ] **步骤 7.3：实现别名感知的工具入口**

**改造 `@app.call_tool()` 装饰的 `call_tool`**：抽公共实现为 `call_tool_impl(name, arguments, alias)`，模块级 handler 变薄包装。将现有 `call_tool` 函数体（从 `try:` 到 `except` 结束）移入 `call_tool_impl`，工具分支改为走别名解析：

```python
async def call_tool_impl(name: str, arguments: dict, alias: str | None) -> list[TextContent]:
    """工具调用公共实现；alias=None 时解析 default_alias/env。"""
    try:
        logger.info(f"Calling tool: {name} with arguments: {arguments} (alias={alias})")

        if name == "execute_sql":
            query = arguments.get("query")
            if not query:
                raise ValueError("Query is required")
            if ";" in query.strip().rstrip(";"):
                return [TextContent(type="text", text=(
                    "Only single statements are supported. "
                    "Instead of USE statements, use fully qualified names: database.table"
                ))]
            resolved = db_config.resolve(alias)
            if resolved is None:
                return [TextContent(type="text", text="未配置任何数据库连接，请访问管理页面 /admin 进行配置。")]
            a, entry = resolved
            return await execute_sql_entry(a, entry, query)

        elif name == "get_schema_info":
            resolved = db_config.resolve(alias)
            if resolved is None:
                return [TextContent(type="text", text="未配置任何数据库连接，请访问管理页面 /admin 进行配置。")]
            a, entry = resolved
            table_name = arguments.get("table_name")
            if table_name:
                db, tbl = parse_table_arg(table_name)
                schema_filter = f"TABLE_SCHEMA = '{db}'" if db else "TABLE_SCHEMA = DATABASE()"
                query = f"SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT FROM information_schema.COLUMNS WHERE {schema_filter} AND TABLE_NAME = '{tbl}' ORDER BY ORDINAL_POSITION"
            else:
                query = "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME, ORDINAL_POSITION"
            return await run_query_entry(entry, query, "read")

        elif name == "get_table_sample":
            resolved = db_config.resolve(alias)
            if resolved is None:
                return [TextContent(type="text", text="未配置任何数据库连接，请访问管理页面 /admin 进行配置。")]
            a, entry = resolved
            db, tbl = parse_table_arg(arguments.get("table_name"))
            limit = min(arguments.get("limit", 5), 20)
            table_ref = f"`{db}`.`{tbl}`" if db else f"`{tbl}`"
            query = f"SELECT * FROM {table_ref} LIMIT {limit}"
            return await run_query_entry(entry, query, "read")

        else:
            raise ValueError(f"Unknown tool: {name}")
    except Exception as e:
        logger.error(f"Error in call_tool: {str(e)}")
        logger.error(traceback.format_exc())
        return [TextContent(type="text", text=f"Error calling tool {name}: {str(e)}")]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatches tool calls from AI agents to the appropriate implementation logic."""
    return await call_tool_impl(name, arguments, alias=None)
```

**改造 `list_resources` / `read_resource` 为别名感知**（模块级函数名保留；现有 env 行为在无 JSON 配置时由 resolve 的 env fallback 保持等价）：

```python
async def list_resources_impl(alias: str | None) -> list[Resource]:
    """Lists tables (or databases) as resources for the resolved alias entry."""
    resolved = db_config.resolve(alias)
    if resolved is None:
        return []
    _, entry = resolved

    def _sync_list():
        with maybe_ssh_tunnel_for(entry) as (host, port):
            config = build_connector_config(entry, "read", host, port)
            try:
                with connect(**config) as conn:
                    with conn.cursor() as cursor:
                        if "database" not in config:
                            cursor.execute("SHOW DATABASES")
                            databases = cursor.fetchall()
                            return [
                                Resource(
                                    uri=f"mysql://database/{db[0]}",
                                    name=f"database_{db[0]}",
                                    mimeType="text/plain",
                                    description=f"MySQL database: {db[0]}"
                                )
                                for db in databases if db[0] not in SYSTEM_DATABASES
                            ]
                        else:
                            cursor.execute("SHOW TABLES")
                            tables = cursor.fetchall()
                            return [
                                Resource(
                                    uri=f"mysql://{table[0]}/data",
                                    name=f"table_{table[0]}",
                                    mimeType="text/plain",
                                    description=f"Data in table: {table[0]}"
                                )
                                for table in tables
                            ]
            except Error as e:
                error_msg = getattr(e, 'msg', None) or str(e) or 'Unknown MySQL error'
                logger.error(f"Failed to list resources: {error_msg}")
                return []

    return await anyio.to_thread.run_sync(_sync_list)


@app.list_resources()
async def list_resources() -> list[Resource]:
    """Lists available MySQL tables (or databases) as resources."""
    return await list_resources_impl(None)
```

`read_resource` 同样模式：实现体移入 `read_resource_impl(alias)`，`config = build_connector_config(entry, "read", host, port)`，`with maybe_ssh_tunnel_for(entry) as (host, port)`，模块级薄包装 `return await read_resource_impl(None)`。资源读取是 SELECT，固定 read 角色。

**新增别名 Server 工厂与注册表**（放在 `read_resource` 实现之后、`list_tools` 之前）：

```python
# ---------------------------------------------------------------------------
# 别名 Server 实例：每个别名独立实例，配置变更后失效重建
# ---------------------------------------------------------------------------

_server_registry: dict[str, Server] = {}


def invalidate_alias(alias: str | None = None) -> None:
    """配置变更后使别名 Server 缓存失效（由 admin_api 回调触发；alias=None 清空全部）。"""
    if alias is None:
        _server_registry.clear()
    else:
        _server_registry.pop(alias, None)


def create_alias_server(alias: str) -> Server:
    """创建绑定指定别名的 MCP Server 实例（SSE 每别名一实例）。"""
    s = Server("mysql_mcp_server")
    s.list_tools()(list_tools)
    s.list_resource_templates()(list_resource_templates)
    s.list_prompts()(list_prompts)
    s.get_prompt()(get_prompt)

    async def _list_resources() -> list[Resource]:
        return await list_resources_impl(alias)

    async def _read_resource(uri: AnyUrl) -> str:
        return await read_resource_impl(alias, uri)

    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        return await call_tool_impl(name, arguments, alias)

    s.list_resources()(_list_resources)
    s.read_resource()(_read_resource)
    s.call_tool()(_call_tool)
    return s
```

注意：`read_resource` 现有签名是 `async def read_resource(uri: AnyUrl) -> str`，改造为 `read_resource_impl(alias: str | None, uri: AnyUrl)`（alias 在前，与 `list_resources_impl` 一致）。

- [ ] **步骤 7.4：改造 `_run_sse_server`——别名路由与挂载 admin**

替换 `_run_sse_server` 中的路由构建部分（`sse = SseServerTransport(...)` 到 `Starlette(routes=...)` 之间）：

```python
    # 每别名独立的 transport 与 server（懒创建）
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse

    _transport_registry: dict[str, SseServerTransport] = {}

    def _make_transport(alias: str) -> SseServerTransport:
        if alias not in _transport_registry:
            endpoint = f"/messages/{alias}/"
            if security_settings is not None:
                _transport_registry[alias] = SseServerTransport(endpoint, security_settings=security_settings)
            else:
                _transport_registry[alias] = SseServerTransport(endpoint)
        return _transport_registry[alias]

    async def handle_sse(request):
        """SSE 端点：/sse?alias=db1 按别名路由。"""
        requested = request.query_params.get("alias")
        resolved = db_config.resolve(requested)
        if resolved is None:
            return PlainTextResponse(
                f"Unknown alias '{requested or ''}'. 请先在管理页面 /admin 配置数据库。", status_code=404)
        alias, _entry = resolved
        transport = _make_transport(alias)
        if alias not in _server_registry:
            _server_registry[alias] = create_alias_server(alias)
        server = _server_registry[alias]
        async with transport.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())
        return Response()

    class AliasMessagesEndpoint:
        """ASGI endpoint：把 /messages/{alias} 的 POST 转发到对应别名的 transport。"""
        async def __call__(self, scope, receive, send):
            alias = scope.get("path_params", {}).get("alias")
            transport = _transport_registry.get(alias)
            if transport is None:
                resp = PlainTextResponse("Unknown alias", status_code=404)
                await resp(scope, receive, send)
                return
            await transport.handle_post_message(scope, receive, send)

    # 配置变更 → 失效别名 Server 缓存
    admin_api.register_on_change(invalidate_alias)

    starlette_app = Starlette(
        routes=[
            Route("/", endpoint=health_check),
            Route("/sse", endpoint=handle_sse),
            Route("/messages/{alias}/", endpoint=AliasMessagesEndpoint()),
            Route("/messages/{alias}", endpoint=AliasMessagesEndpoint()),
            Mount("/admin", app=admin_api.create_admin_app(), name="admin"),
        ]
    )
```

同时删除原来的单例 `sse = SseServerTransport("/messages/", ...)` 与 `Mount("/messages/", app=sse.handle_post_message)` 及旧 `handle_sse`。`security_settings` 构建逻辑保持不变。

`_run_sse_server` 顶部 import 行相应调整：`from starlette.responses import Response, PlainTextResponse`、`from starlette.requests import Request`。

- [ ] **步骤 7.5：运行全部测试**

运行：`.venv\Scripts\python.exe -m pytest -v`
预期：全部 PASS（含原有 25+1 skip）

- [ ] **步骤 7.6：启动冒烟验证**

```
.venv\Scripts\python.exe -m mysql_mcp_server
```

（需设置 `MCP_TRANSPORT=sse`，用新终端）：
```powershell
$env:MCP_TRANSPORT="sse"; $env:MCP_SSE_PORT="8000"; .venv\Scripts\python.exe -m mysql_mcp_server
```

另开终端验证：
```powershell
curl.exe http://127.0.0.1:8000/api/health
curl.exe http://127.0.0.1:8000/sse?alias=nope   # 预期 404 提示配置
```
预期：health 返回 `{"status":"ok","databases":0}`；未知别名 404。验证后停止服务。

- [ ] **步骤 7.7：Commit**

```
git add src/mysql_mcp_server/server.py tests/test_execute_policy.py
git commit -m "feat: per-alias SSE routing with admin page mounted on single process"
```

---

### 任务 8：README 与全量回归

**文件：**
- 修改：`README.md`

- [ ] **步骤 8.1：README 增加"管理页面与多数据库"章节**

在 `## Configuration` 章节之后插入：

```markdown
## Admin Page & Multi-Database Aliases (SSE mode)

Run the server in SSE mode and open the built-in admin page to manage multiple
database connections, each with **separate read/write accounts**:

```bash
MCP_TRANSPORT=sse MCP_SSE_PORT=8000 python -m mysql_mcp_server
# Admin page: http://127.0.0.1:8000/admin/  (loopback only)
```

For each alias you configure:

| Field | Purpose |
|---|---|
| 连接 (host/port/database) | Where to connect |
| 查询用户 (read_user) | Used for SELECT/SHOW/DESCRIBE/EXPLAIN |
| 操作用户 (write_user) | Used for writes **after confirmation** |
| write_policy | `client_confirm` (default) or `elicitation_only` |
| allow_delete | Master switch for DELETE/TRUNCATE/DROP (default off) |

Clients connect per alias: `http://127.0.0.1:8000/sse?alias=db1`
(omit `alias` to use the default alias; plain `MYSQL_*` env vars still work as a
backward-compatible single-database fallback).

Write operations are confirmed through MCP **elicitation** when the client
supports it; otherwise the per-alias `write_policy` decides whether to trust the
client-side tool-confirmation UI (`client_confirm`) or reject the write
(`elicitation_only`). All writes are recorded in the admin page's audit list.
```

- [ ] **步骤 8.2：全量回归**

运行：`.venv\Scripts\python.exe -m pytest -v`
预期：全部 PASS，无新增 skip

- [ ] **步骤 8.3：Commit**

```
git add README.md
git commit -m "docs: document admin page, aliases, and dual-account confirmation"
```

---

## 计划自检结果

1. **规格覆盖度**：规格 §3（架构/别名路由/JSON 配置/env 兼容）→ 任务 2、7；§4（三级判定/双通道确认/删除权限/审计）→ 任务 1、6；§5（UI/API/回环校验/原子写/静态单文件）→ 任务 4、5；§6（测试策略）→ 各任务内联 + 任务 8 回归。无遗漏。
2. **占位符扫描**：无"待定/TODO/类似任务N"；所有代码步骤含完整代码。
3. **类型一致性**：`resolve() -> tuple[str, dict] | None`、`normalize_entry/validate_entry(entry)`、`run_query_entry(entry, query, role)`、`call_tool_impl(name, arguments, alias)`、`execute_sql_entry(alias, entry, query)`、`read_resource_impl(alias, uri)`、`invalidate_alias(alias=None)` 各任务间签名一致；前端 fetch 路径与 admin_api 路由一致。
