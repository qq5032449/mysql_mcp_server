"""写操作审计：内存最近 200 条 + 追加文件日志（带大小轮转，文件失败不影响主流程）。

每条记录含执行结果状态（executed/error/拒绝原因）、耗时与影响行数；
读操作可通过 MYSQL_MCP_AUDIT_READ=true 一并记录。
"""

from collections import deque
from datetime import datetime
from pathlib import Path
from threading import Lock

LOG_DIR = Path("logs")
LOG_FILE = "audit.log"
MAX_FILE_BYTES = 5 * 1024 * 1024  # 超过后轮转为 audit.log.1（仅保留一代）
MEM_MAX = 200

_entries: deque = deque(maxlen=MEM_MAX)
_lock = Lock()


def record(alias: str, sql_type: str, channel: str, sql: str, status: str = "executed",
           elapsed_ms: int | None = None, rows: int | None = None) -> dict:
    """记录一条审计并返回该条目。elapsed_ms/rows 缺省为 None（拒绝类调用无执行数据）。"""
    entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "alias": alias,
        "type": sql_type,
        "channel": channel,
        "status": status,
        "sql": sql[:200],
        "elapsed_ms": elapsed_ms,
        "rows": rows,
    }
    with _lock:
        _entries.append(entry)
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = LOG_DIR / LOG_FILE
            _rotate_if_needed(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{entry['time']} | {entry['alias']} | {entry['type']} | "
                        f"{entry['channel']} | {entry['status']} | "
                        f"{_fmt_ms(elapsed_ms)} | {_fmt_rows(rows)} | {entry['sql']}\n")
        except OSError:
            pass
    return entry


def _fmt_ms(elapsed_ms: int | None) -> str:
    return "-" if elapsed_ms is None else f"{elapsed_ms}ms"


def _fmt_rows(rows: int | None) -> str:
    return "-" if rows is None else str(rows)


def _rotate_if_needed(path: Path) -> None:
    """文件超过上限时轮转：audit.log → audit.log.1（覆盖上一代，os.replace 跨平台可覆盖）。"""
    try:
        if path.exists() and path.stat().st_size >= MAX_FILE_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass


def list_entries() -> list[dict]:
    with _lock:
        return list(_entries)


def clear() -> None:
    with _lock:
        _entries.clear()
