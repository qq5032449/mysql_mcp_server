"""SQL 语句三级判定：read / write / delete。

规则（见设计文档 4.1-4.2 节）：
- 去掉注释后取首个关键字判定
- WITH (CTE) 语句取括号深度 0 处首个出现的主语句关键字
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
    sql = re.sub(r"/\*.*?\*/", "  ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", "  ", sql)
    sql = re.sub(r"#[^\n]*", "  ", sql)
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
    """WITH (CTE) 语句：返回顶层首个出现的主语句关键字（大写）；无则空串。"""
    for tok in _top_level_tokens(strip_comments(sql)):
        if tok.upper() in _CTE_MAIN_KEYWORDS:
            return tok.upper()
    return ""


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
