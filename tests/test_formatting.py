import pytest
from unittest.mock import MagicMock, patch
from mysql_mcp_server import db_config
from mysql_mcp_server.server import call_tool
from mcp.types import TextContent


@pytest.fixture(autouse=True)
def _env_config(tmp_path, monkeypatch):
    """别名路由走 db_config.resolve：提供 env 回退条目 + 空配置文件。"""
    monkeypatch.setenv("MYSQL_USER", "u")
    monkeypatch.setenv("MYSQL_PASSWORD", "p")
    monkeypatch.setattr(db_config, "CONFIG_FILE", tmp_path / "databases.json")
    monkeypatch.setattr(db_config, "CONFIG_DIR", tmp_path)
    db_config.reset_cache()
    yield
    db_config.reset_cache()


@pytest.mark.asyncio
@patch("mysql_mcp_server.db_drivers.connect_entry")
async def test_call_tool_describe_formatting(mock_connect):
    """Test that DESCRIBE queries are formatted correctly with NULL handling."""
    # Mock cursor behavior
    mock_cursor = MagicMock()
    mock_cursor.description = [("Field",), ("Type",), ("Null",)]
    # Simulate DESCRIBE output: (Field, Type, Null)
    mock_cursor.fetchall.return_value = [
        ("id", "int", "NO"),
        ("name", "varchar", "YES"),
        ("extra", "text", None) # Test NULL handling
    ]
    
    mock_conn = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn
    
    response = await call_tool("execute_sql", {"query": "DESCRIBE users"})
    
    assert len(response) == 1
    assert isinstance(response[0], TextContent)
    
    lines = response[0].text.split("\n")
    assert lines[0] == "Field,Type,Null"
    assert lines[1] == "id,int,NO"
    assert lines[2] == "name,varchar,YES"
    assert lines[3] == "extra,text,NULL" # NULL should be converted to string "NULL"

@pytest.mark.asyncio
@patch("mysql_mcp_server.db_drivers.connect_entry")
async def test_call_tool_empty_results(mock_connect):
    """Test handling of queries that return no results."""
    mock_cursor = MagicMock()
    mock_cursor.description = [("id",)]
    mock_cursor.fetchall.return_value = []
    
    mock_conn = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn
    
    response = await call_tool("execute_sql", {"query": "SELECT * FROM empty_table"})
    
    assert len(response) == 1
    assert "No results returned" in response[0].text

@pytest.mark.asyncio
@patch("mysql_mcp_server.db_drivers.connect_entry")
async def test_call_tool_show_tables(mock_connect, monkeypatch):
    """Test SHOW TABLES formatting."""
    monkeypatch.setenv("MYSQL_DATABASE", "test_db")

    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [("users",), ("orders",)]
    
    mock_conn = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn
    
    response = await call_tool("execute_sql", {"query": "SHOW TABLES"})
    
    assert len(response) == 1
    assert "Tables_in_test_db" in response[0].text
    assert "users" in response[0].text
    assert "orders" in response[0].text

@pytest.mark.asyncio
@patch("mysql_mcp_server.db_drivers.connect_entry")
async def test_list_resources_identifier_safe(mock_connect, monkeypatch):
    """Test that resources have identifier-safe names for strict LLMs (Issue #39)."""
    from mysql_mcp_server.server import list_resources

    # Mock for single-database mode
    monkeypatch.setenv("MYSQL_DATABASE", "test_db")
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [("users",), ("products",)]
    
    mock_conn = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn
    
    resources = await list_resources()
    
    assert len(resources) == 2
    # Should be table_name, not "Table: name"
    assert resources[0].name == "table_users"
    assert resources[1].name == "table_products"
    assert str(resources[0].uri) == "mysql://users/data"

@pytest.mark.asyncio
@patch("mysql_mcp_server.db_drivers.connect_entry")
async def test_list_resources_multi_db_safe(mock_connect, monkeypatch):
    """Test that database resources have identifier-safe names."""
    from mysql_mcp_server.server import list_resources

    # Multi-database mode: no MYSQL_DATABASE in env entry
    monkeypatch.delenv("MYSQL_DATABASE", raising=False)
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [("db1",), ("db2",), ("information_schema",)]
    
    mock_conn = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn
    
    resources = await list_resources()
    
    # information_schema should be filtered out
    assert len(resources) == 2
    # Should be database_name
    assert resources[0].name == "database_db1"
    assert resources[1].name == "database_db2"
    assert str(resources[0].uri) == "mysql://database/db1"
