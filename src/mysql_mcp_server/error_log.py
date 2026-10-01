"""执行错误入库：SQLite 持久化 + 内存最近 100 条 + 文件兜底（失败不影响主流程）。

只记录真正打到数据库并返回错误的调用（run_query_entry 与资源读取/列表的
DB 异常路径，调用方传入别名、工具名与 SQL）。
确认策略拦截（未开启删除权限、令牌无效/过期、用户拒绝、elicitation_only 等）
在执行前即返回，不会进入本模块；数据库返回的 UPDATE/DELETE 无权限错误
（command denied）也在入库前过滤。
"""

import logging
import os
import sqlite3
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

logger = logging.getLogger("mysql_mcp_server.error_log")

DB_FILE = Path("logs") / "error_log.db"
FALLBACK_FILE = Path("logs") / "error_log_fallback.log"

TABLE_NAME = "mcp_error_log"
SQL_MAX_LEN = 2000
ERR_MAX_LEN = 2000
DEFAULT_RETENTION_DAYS = 90  # 保留期（天），MYSQL_MCP_ERROR_RETENTION_DAYS 可覆盖，0 表示永久保留

_entries: deque = deque(maxlen=100)
_lock = Lock()
_last_cleanup_date: str | None = None  # 每自然日最多跑一次清理


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def retention_days() -> int:
    try:
        return max(0, int(os.getenv("MYSQL_MCP_ERROR_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))))
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def cleanup_old(days: int | None = None, conn: sqlite3.Connection | None = None) -> int:
    """删除超过保留期的记录，返回删除行数；days=0 表示跳过。可复用外部连接。"""
    if days is None:
        days = retention_days()
    if days <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    own_conn = conn is None
    try:
        if own_conn:
            DB_FILE.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(DB_FILE), timeout=5)
        cur = conn.execute(f"DELETE FROM {TABLE_NAME} WHERE time < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Error log cleanup failed: {e}")
        return 0
    finally:
        if own_conn and conn is not None:
            conn.close()


def _maybe_daily_cleanup(conn: sqlite3.Connection) -> None:
    """每自然日首个写入/查询触发一次保留期清理（廉价惰性任务）。"""
    global _last_cleanup_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _last_cleanup_date == today:
        return
    _last_cleanup_date = today
    cleanup_old(retention_days(), conn=conn)


def is_permission_error(db_error: str) -> bool:
    """数据库返回的 UPDATE/DELETE 无权限错误：不入库。

    如 MySQL：UPDATE command denied to user 'w'@'%' for table 't'。
    """
    msg = (db_error or "").lower()
    if "command denied" not in msg:
        return False
    return "update" in msg or "delete" in msg


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT NOT NULL,
            alias TEXT NOT NULL DEFAULT '',
            tool TEXT NOT NULL DEFAULT '',
            sql_type TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            sql TEXT NOT NULL DEFAULT '',
            db_error TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_time ON {TABLE_NAME}(time)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_alias ON {TABLE_NAME}(alias)")


def record(alias: str, tool: str, sql: str, db_error: str,
           sql_type: str = "-", role: str = "-") -> dict | None:
    """记录一条执行错误并返回该条目；UPDATE/DELETE 无权限错误返回 None 且不入库。"""
    if is_permission_error(db_error):
        return None
    entry = {
        "time": _now_str(),
        "alias": alias or "-",
        "tool": tool or "-",
        "type": sql_type or "-",
        "role": role or "-",
        "sql": (sql or "")[:SQL_MAX_LEN],
        "db_error": (db_error or "")[:ERR_MAX_LEN],
    }
    with _lock:
        _entries.append(entry)
        try:
            DB_FILE.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(DB_FILE), timeout=5)
            try:
                _ensure_schema(conn)
                _maybe_daily_cleanup(conn)
                cur = conn.execute(
                    f"INSERT INTO {TABLE_NAME} (time, alias, tool, sql_type, role, sql, db_error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (entry["time"], entry["alias"], entry["tool"], entry["type"],
                     entry["role"], entry["sql"], entry["db_error"]),
                )
                entry["id"] = cur.lastrowid
                conn.commit()
            finally:
                conn.close()
        except (OSError, sqlite3.Error) as e:
            logger.warning(f"Failed to persist error log: {e}")
            try:
                FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(FALLBACK_FILE, "a", encoding="utf-8") as f:
                    f.write(f"{entry['time']} | {entry['alias']} | {entry['tool']} | "
                            f"{entry['type']} | {entry['role']} | "
                            f"{entry['sql']} | {entry['db_error']}\n")
            except OSError:
                pass
    return entry


def query_errors(limit: int = 100, alias: str | None = None) -> list[dict]:
    """查持久化错误（最新在前）；库文件不可用时回退到内存最近记录。"""
    try:
        limit = max(1, min(int(limit or 100), 500))
    except (TypeError, ValueError):
        limit = 100
    with _lock:
        memory = list(reversed(_entries))
        try:
            if not DB_FILE.exists():
                return memory[:limit]
            conn = sqlite3.connect(str(DB_FILE), timeout=5)
            try:
                _ensure_schema(conn)
                _maybe_daily_cleanup(conn)
                conn.row_factory = sqlite3.Row
                if alias:
                    rows = conn.execute(
                        f"SELECT * FROM {TABLE_NAME} WHERE alias = ? "
                        "ORDER BY id DESC LIMIT ?",
                        (alias, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT * FROM {TABLE_NAME} ORDER BY id DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                return [
                    {"id": r["id"], "time": r["time"], "alias": r["alias"],
                     "tool": r["tool"], "type": r["sql_type"], "role": r["role"],
                     "sql": r["sql"], "db_error": r["db_error"]}
                    for r in rows
                ]
            finally:
                conn.close()
        except (OSError, sqlite3.Error) as e:
            logger.warning(f"Failed to query error log: {e}")
            if alias:
                return [e for e in memory if e["alias"] == alias][:limit]
            return memory[:limit]


def list_entries() -> list[dict]:
    with _lock:
        return list(_entries)


def clear() -> None:
    with _lock:
        _entries.clear()
