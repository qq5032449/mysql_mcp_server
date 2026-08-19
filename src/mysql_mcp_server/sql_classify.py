"""SQL 语句三级判定：read / write / delete。

规则（见设计文档 4.1-4.2 节）：
- 去掉注释后取首个关键字判定
- WITH (CTE) 语句取括号深度 0 处首个出现的主语句关键字
- 无法判定的一律按 write 处理（安全兜底）
"""

import re

READ_PREFIXES = {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
# 仅作文档用途：列出常见写操作前缀便于阅读。判定逻辑未显式查询该集合，
# 未识别的关键字一律走 classify 的默认 write 兜底分支。
WRITE_PREFIXES = {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP",
                  "TRUNCATE", "RENAME", "GRANT", "REVOKE", "SET", "LOCK", "UNLOCK",
                  "CALL", "LOAD", "OPTIMIZE", "ANALYZE", "REPAIR", "FLUSH", "RESET", "KILL"}
DELETE_PREFIXES = {"DELETE", "TRUNCATE", "DROP"}

_CTE_MAIN_KEYWORDS = {"SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE", "DROP", "TRUNCATE"}
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_QUOTES = ("'", '"', "`")
# EXPLAIN ANALYZE（MySQL 8.0.18+）会真正执行目标语句，须按剥离前缀后的实际语句递归判定
_EXPLAIN_ANALYZE_RE = re.compile(r"^\s*EXPLAIN\s+ANALYZE\b", re.IGNORECASE)


def _skip_quoted(text: str, i: int) -> int:
    """text[i] 是引号字符：返回整个引号段的结束位置（含闭合引号）；未闭合返回 len(text)。

    处理反斜杠转义（\\'、\\"、\\`）与反引号内的双反引号转义（`a``b`）。
    """
    quote = text[i]
    j = i + 1
    n = len(text)
    while j < n:
        c = text[j]
        if c == "\\" and j + 1 < n:
            j += 2
        elif quote == "`" and c == "`" and j + 1 < n and text[j + 1] == "`":
            j += 2
        elif c == quote:
            return j + 1
        else:
            j += 1
    return n


def strip_comments(sql: str) -> str:
    """去掉 -- 行注释、# 行注释、块注释；字符串/反引号引号内的注释符号不剥。"""
    out = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch in _QUOTES:
            j = _skip_quoted(sql, i)
            out.append(sql[i:j])
            i = j
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append("  ")
            i = j
        elif ch == "#":
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append("  ")
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j == -1:
                out.append(ch)
                i += 1
            else:
                out.append("  ")
                i = j + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def first_keyword(sql: str) -> str:
    """返回去掉注释与左括号后的首个关键字（大写）；无则空串。"""
    text = strip_comments(sql).strip().lstrip("(").strip()
    m = re.match(r"[A-Za-z]+", text)
    return m.group(0).upper() if m else ""


def _top_level_tokens(text: str):
    """产出括号深度 0 处的 token（标识符整词匹配，避免误配列名子串）。

    带引号状态机：'、"、` 引号段内的内容不产出 token、不计括号深度，
    防止字符串字面量里的 ) 提前归零深度或关键字出现在"顶层"。
    """
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _QUOTES:
            i = _skip_quoted(text, i)
        elif ch == "(":
            depth += 1
            i += 1
        elif ch == ")":
            depth = max(0, depth - 1)
            i += 1
        elif depth == 0:
            m = _WORD_RE.match(text, i)
            if m:
                yield m.group(0)
                i = m.end()
            else:
                yield ch
                i += 1
        else:
            i += 1


def cte_main_keyword(sql: str) -> str:
    """WITH (CTE) 语句：返回顶层首个出现的主语句关键字（大写）；无则空串。

    取"首个"而非"末尾"：如 `INSERT INTO t SELECT ...` 末尾的 SELECT 括号深度
    也是 0，取末尾会把 write 误判为 read；首个主语句关键字即主语句类型。
    """
    for tok in _top_level_tokens(strip_comments(sql)):
        if tok.upper() in _CTE_MAIN_KEYWORDS:
            return tok.upper()
    return ""


def classify(sql: str) -> str:
    """判定 SQL 语句类型：'read' | 'write' | 'delete'。默认 'write'（安全兜底）。"""
    text = strip_comments(sql)
    m = _EXPLAIN_ANALYZE_RE.match(text)
    if m:
        # EXPLAIN ANALYZE 会真正执行目标语句，须按实际语句判定（普通 EXPLAIN
        # 与 EXPLAIN FORMAT=... 不执行语句，仍走 read 分支）
        return classify(text[m.end():])
    kw = first_keyword(text)
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
