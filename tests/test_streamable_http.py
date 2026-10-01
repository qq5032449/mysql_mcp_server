"""Streamable HTTP 传输（/mcp，stateless）：tools/list、未知别名 404、GET 行为。"""

import pytest
from starlette.testclient import TestClient

from mysql_mcp_server import db_config
from mysql_mcp_server.server import build_starlette_app


@pytest.fixture
def sse_app(tmp_path, monkeypatch):
    monkeypatch.setattr(db_config, "CONFIG_FILE", tmp_path / "databases.json")
    monkeypatch.setattr(db_config, "CONFIG_DIR", tmp_path)
    for k in ("MYSQL_USER", "MYSQL_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    db_config.reset_cache()
    db_config.save_config({
        "default_alias": "db1",
        "databases": {"db1": {
            "host": "localhost", "port": 3306, "database": "mydb",
            "read_user": {"user": "r", "password": "rp"},
            "write_user": {"user": "w", "password": "wp"},
        }},
    })
    yield build_starlette_app()
    db_config.reset_cache()


def _client(app):
    # 回环校验：TestClient 默认 host 是 "testclient"，显式传 127.0.0.1
    return TestClient(app, client=("127.0.0.1", 50000))


def _rpc(method, id_=1, **params):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}


def test_mcp_tools_list_stateless(sse_app):
    """stateless 模式：无需 initialize 握手，单次 POST 直接返回工具列表。"""
    c = _client(sse_app)
    r = c.post("/mcp", json=_rpc("tools/list"), headers={
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    })
    assert r.status_code == 200
    body = r.json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert names == {"execute_sql", "get_schema_info", "get_table_sample", "search_schema"}


def test_mcp_unknown_alias_404(sse_app):
    c = _client(sse_app)
    r = c.post("/mcp?alias=nope", json=_rpc("tools/list"), headers={
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    })
    assert r.status_code == 404


def test_mcp_get_returns_405(sse_app):
    """stateless 模式只支持 POST：GET 直接 405（不进入 SDK 的 SSE 等待）。"""
    c = _client(sse_app)
    r = c.get("/mcp", headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 405


def test_mcp_delete_returns_405(sse_app):
    c = _client(sse_app)
    r = c.delete("/mcp", headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 405


def test_mcp_alias_query_resolves(sse_app):
    """?alias=db1 正常解析（别名路由与 SSE 一致）。"""
    c = _client(sse_app)
    r = c.post("/mcp?alias=db1", json=_rpc("tools/list"), headers={
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    })
    assert r.status_code == 200
