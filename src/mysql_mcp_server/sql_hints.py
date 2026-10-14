"""执行失败后的可操作提示：把数据库报错补成 AI 能自纠的中文提示。

两类增强（只在出错时触发，成功路径零开销）：
- 「列不存在」（MySQL `Unknown column` / 达梦 `Invalid column name`）：从 SQL 里
  提取涉及的表，查出真实列名附在错误后，AI 无需再问一轮就能改对。
- 「SQL 语法错误」：提示标识符引用符（MySQL 反引号 / 达梦双引号），
  覆盖库名或表名含连字符、中文、保留字导致语法错的常见情况。
"""

import logging
import re

from mysql_mcp_server import conn_pool, db_drivers

logger = logging.getLogger("mysql_mcp_server.sql_hints")

MAX_TABLES = 3        # 最多补几张表的列清单
MAX_COLUMNS = 40      # 每张表最多列出的列数
MAX_TOTAL_COLUMNS = 120

_UNKNOWN_COLUMN_RE = re.compile(r"Unknown column '([^']+)'", re.I)
_DM_BAD_COLUMN_RE = re.compile(r"Invalid column name \[([^\]]+)\]", re.I)
# MySQL: "You have an error in your SQL syntax"；达梦: "[CODE:-2007]Error in line: 1 / Syntax error near ..."
_SYNTAX_ERROR_RE = re.compile(r"error in your SQL syntax|syntax error", re.I)
# 达梦数据字典缺 COMMENTS 列时的报错（用于 schema 查询降级重试）
_MISSING_COMMENTS_RE = re.compile(
    r"(?:Unknown column|Invalid column name)[^\n]{0,60}COMMENTS", re.I)

# 可带库/模式前缀、可加引用符的标识符（含中文与连字符）
_IDENT = r"[`\"\[]?[A-Za-z0-9_$\-\u4e00-\u9fff]+[`\"\]]?"
_QUALIFIED = rf"(?:{_IDENT}\s*\.\s*)?{_IDENT}"
_TABLE_RE = re.compile(rf"\b(?:FROM|JOIN|UPDATE|INTO)\s+({_QUALIFIED})", re.I)

_SYSTEM_SCHEMAS = {
    "information_schema", "mysql", "performance_schema", "sys",
    "SYS", "SYSSSO", "SYSAUDITOR", "CTISYS",
}


def unknown_column_name(db_error: str) -> str | None:
    """从错误里取出缺失的列名；不是列不存在类错误时返回 None。"""
    m = _UNKNOWN_COLUMN_RE.search(db_error or "") or _DM_BAD_COLUMN_RE.search(db_error or "")
    return m.group(1) if m else None


def is_syntax_error(db_error: str) -> bool:
    return bool(_SYNTAX_ERROR_RE.search(db_error or ""))


def is_missing_comments_error(db_error: str) -> bool:
    """达梦数据字典缺 COMMENTS 列的报错（schema 查询需降级重试）。"""
    return bool(_MISSING_COMMENTS_RE.search(db_error or ""))


def _strip_ident(name: str) -> str:
    return name.strip().strip('`"[]')


def extract_tables(query: str) -> list[tuple[str | None, str]]:
    """提取 SQL 中 FROM/JOIN/UPDATE/INTO 后的表（去重、保序，最多 MAX_TABLES 张）。

    返回 [(库名或 None, 表名)]；跳过系统库与子查询。
    """
    out: list[tuple[str | None, str]] = []
    for m in _TABLE_RE.finditer(query or ""):
        raw = m.group(1)
        parts = [_strip_ident(p) for p in raw.split(".")]
        if len(parts) == 2:
            db, table = parts
        else:
            db, table = None, parts[-1]
        if not table or table.upper() in ("SELECT", "DUAL", "AS"):
            continue
        if db and db.lower() in {s.lower() for s in _SYSTEM_SCHEMAS}:
            continue
        item = (db, table)
        if item not in out:
            out.append(item)
        if len(out) >= MAX_TABLES:
            break
    return out


def _fetch_columns(entry: dict, db: str | None, table: str) -> list[str]:
    """查单表真实列名；任何失败都返回空列表（提示功能不能反过来打断主流程）。"""
    try:
        with conn_pool.get_connection(entry, "read") as conn:
            with conn.cursor() as cursor:
                cursor.execute(db_drivers.column_names_sql(table, db, entry))
                return [str(r[0]) for r in cursor.fetchall() if r and r[0]]
    except Exception as e:
        logger.debug("column hint lookup failed for %s: %s", table, e)
        return []


def _column_hint(entry: dict, query: str) -> str:
    tables = extract_tables(query)
    if not tables:
        return ""
    lines = []
    total = 0
    for db, table in tables:
        cols = _fetch_columns(entry, db, table)
        if not cols:
            continue
        total += len(cols)
        shown = cols[:MAX_COLUMNS]
        more = "" if len(cols) <= MAX_COLUMNS else f" …（共 {len(cols)} 列）"
        label = f"{db}.{table}" if db else table
        lines.append(f"- 表 {label} 的实际列：{', '.join(shown)}{more}")
        if total >= MAX_TOTAL_COLUMNS:
            break
    if not lines:
        return ""
    return "参考（请据此修正 SQL 后重试，勿再猜列名）：\n" + "\n".join(lines)


def augment_error(entry: dict, query: str, db_error: str) -> str:
    """给数据库报错补上可操作提示；无适用提示时原样返回。"""
    if not db_error:
        return db_error
    # 数据字典缺 COMMENTS 是服务端自身元数据查询的问题（调用方会降级重试），
    # 不是用户的 SQL 写错，跳过提示避免对 ALL_TAB_COLUMNS 之类再查一遍列。
    if is_missing_comments_error(db_error):
        return db_error
    hints = []
    if unknown_column_name(db_error):
        hint = _column_hint(entry, query)
        if hint:
            hints.append(hint)
    if is_syntax_error(db_error):
        hints.append("提示：" + db_drivers.quote_ident_hint(entry))
    if not hints:
        return db_error
    return db_error + "\n\n" + "\n".join(hints)
