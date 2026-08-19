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
