"""多数据库驱动抽象层：MySQL 与达梦（DM8）的连接与方言差异收敛点。

设计：
- DB API 2.0 兼容：两种驱动都暴露 connect / cursor / execute / fetchall /
  description / commit / 上下文管理器，执行路径可共用同一套代码。
- 驱动按需延迟导入：未安装 dmPython 的环境仍可正常使用 MySQL；
  只有实际连接达梦条目时才要求 dmPython 可用。
- 方言差异（标识符引用、元数据查询、采样分页）由本模块的纯函数提供，
  便于单测覆盖，无需真实数据库。
"""

import logging
import os

logger = logging.getLogger("mysql_mcp_server.db_drivers")

MYSQL = "mysql"
DAMENG = "dameng"

VALID_DB_TYPES = (MYSQL, DAMENG)

DEFAULT_PORTS = {MYSQL: 3306, DAMENG: 5236}

_DM_SYSTEM_SCHEMAS = frozenset({"SYS", "SYSSSO", "SYSAUDITOR", "CTISYS"})


def normalize_db_type(value) -> str:
    """归一化数据库类型；空值/未知值回退为 mysql（老配置零迁移）。"""
    if isinstance(value, str) and value.strip().lower() == DAMENG:
        return DAMENG
    return MYSQL


def entry_db_type(entry: dict) -> str:
    return normalize_db_type((entry or {}).get("db_type"))


def default_port(entry: dict) -> int:
    return DEFAULT_PORTS[entry_db_type(entry)]


def quote_ident(name: str, entry: dict) -> str:
    """按方言引用标识符：MySQL 反引号，达梦双引号。"""
    if entry_db_type(entry) == DAMENG:
        return '"' + name.replace('"', '""') + '"'
    return "`" + name.replace("`", "``") + "`"


def qualified_table(db: str | None, tbl: str, entry: dict) -> str:
    if db:
        return f"{quote_ident(db, entry)}.{quote_ident(tbl, entry)}"
    return quote_ident(tbl, entry)


def qstr(value: str) -> str:
    """SQL 字符串字面量转义（防单引号注入，配置值直拼 SQL 时使用）。"""
    return "'" + str(value).replace("'", "''") + "'"


def like_pattern(value: str) -> str:
    """LIKE 匹配串转义：单引号、反斜杠与 %/_ 通配符，防止关键字里带通配符注入。"""
    s = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return qstr(s)


def _dameng_owner(entry: dict, db: str | None) -> str | None:
    """达梦默认模式：显式 db > 条目 database（连接 schema）> 读账号（登录默认模式）。"""
    owner = db or entry.get("database") or (entry.get("read_user") or {}).get("user")
    return owner or None


def _dameng_owner_filt(owner: str | None, alias: str = "") -> str:
    """达梦 OWNER 过滤片段；alias 为表别名（如 "C"），空串表示不加前缀。"""
    prefix = f"{alias}." if alias else ""
    return f" AND UPPER({prefix}OWNER) = UPPER({qstr(owner)})" if owner else ""


# 达梦列注释不在 ALL_TAB_COLUMNS 里（部分版本该列不存在），统一走 ALL_COL_COMMENTS
# 左连接取；缺失时该列为 NULL，不影响其它元数据。
_DM_COL_COMMENTS_JOIN = (
    "FROM ALL_TAB_COLUMNS C LEFT JOIN ALL_COL_COMMENTS M "
    "ON M.OWNER = C.OWNER AND M.TABLE_NAME = C.TABLE_NAME AND M.COLUMN_NAME = C.COLUMN_NAME "
)


def schema_sql(table: str | None, db: str | None, entry: dict) -> str:
    """构造列元数据查询；两种方言统一输出五列：列名、类型、可空、默认值、注释。"""
    if entry_db_type(entry) == DAMENG:
        # 达梦标识符默认大写存储，UPPER 比较兼容大小写写法
        owner = _dameng_owner(entry, db)
        owner_filt = _dameng_owner_filt(owner, "C")
        if table:
            return (
                "SELECT C.COLUMN_NAME, C.DATA_TYPE, C.NULLABLE, C.DATA_DEFAULT, M.COMMENTS "
                f"{_DM_COL_COMMENTS_JOIN}"
                f"WHERE UPPER(C.TABLE_NAME) = UPPER({qstr(table)}){owner_filt} "
                "ORDER BY C.COLUMN_ID"
            )
        return (
            "SELECT C.TABLE_NAME, C.COLUMN_NAME, C.DATA_TYPE, C.NULLABLE "
            f"FROM ALL_TAB_COLUMNS C WHERE 1=1{owner_filt} "
            "ORDER BY C.TABLE_NAME, C.COLUMN_ID"
        )
    if table:
        schema_filter = f"TABLE_SCHEMA = {qstr(db)}" if db else "TABLE_SCHEMA = DATABASE()"
        return (
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT "
            f"FROM information_schema.COLUMNS WHERE {schema_filter} "
            f"AND TABLE_NAME = {qstr(table)} ORDER BY ORDINAL_POSITION"
        )
    return (
        "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION"
    )


def schema_sql_without_comments(table: str, db: str | None, entry: dict) -> str:
    """列元数据查询的降级版（不含注释列），用于老版本达梦数据字典缺 COMMENTS 时重试。

    MySQL 的 COLUMN_COMMENT 始终存在，直接复用 schema_sql。
    """
    if entry_db_type(entry) != DAMENG:
        return schema_sql(table, db, entry)
    owner_filt = _dameng_owner_filt(_dameng_owner(entry, db), "")
    return (
        "SELECT COLUMN_NAME, DATA_TYPE, NULLABLE, DATA_DEFAULT "
        f"FROM ALL_TAB_COLUMNS WHERE UPPER(TABLE_NAME) = UPPER({qstr(table)})"
        f"{owner_filt} ORDER BY COLUMN_ID"
    )


def column_names_sql(table: str, db: str | None, entry: dict) -> str:
    """单表列名清单（单列输出），用于把「列不存在」的错误提示补上真实列。"""
    if entry_db_type(entry) == DAMENG:
        owner_filt = _dameng_owner_filt(_dameng_owner(entry, db), "")
        return (
            "SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS "
            f"WHERE UPPER(TABLE_NAME) = UPPER({qstr(table)}){owner_filt} ORDER BY COLUMN_ID"
        )
    schema_filter = f"TABLE_SCHEMA = {qstr(db)}" if db else "TABLE_SCHEMA = DATABASE()"
    return (
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        f"WHERE {schema_filter} AND TABLE_NAME = {qstr(table)} ORDER BY ORDINAL_POSITION"
    )


def quote_ident_hint(entry: dict) -> str:
    """标识符引用符提示（含方言说明），供语法错误时附在错误信息后。"""
    if entry_db_type(entry) == DAMENG:
        return ('库名/表名/别名含连字符、中文或与保留字同名时需加双引号，'
                '例如 "MY-SCHEMA"."ORDER"')
    return ('库名/表名/别名含连字符、中文或与保留字同名时需加反引号，'
            '例如 `medical-my`.`consultation`')


def sample_sql(db: str | None, tbl: str, limit: int, entry: dict) -> str:
    """构造采样查询；DM8 支持 LIMIT 子句，与 MySQL 共用同一写法。"""
    return f"SELECT * FROM {qualified_table(db, tbl, entry)} LIMIT {limit}"


def list_tables_sql(entry: dict) -> str:
    """列出当前库/模式下的表：MySQL 用 SHOW TABLES，达梦查 USER_TABLES。"""
    if entry_db_type(entry) == DAMENG:
        return "SELECT TABLE_NAME FROM USER_TABLES ORDER BY TABLE_NAME"
    return "SHOW TABLES"


def list_schemas_sql(entry: dict) -> str:
    """多库/多模式模式下列出可访问的模式：MySQL 用 SHOW DATABASES，达梦查 ALL_USERS（过滤系统用户）。"""
    if entry_db_type(entry) == DAMENG:
        names = ", ".join(qstr(n) for n in sorted(_DM_SYSTEM_SCHEMAS))
        return f"SELECT USERNAME FROM ALL_USERS WHERE USERNAME NOT IN ({names}) ORDER BY USERNAME"
    return "SHOW DATABASES"


def search_schema_sql(keyword: str, limit: int, entry: dict) -> str:
    """按关键字搜索表名/列名/注释，返回统一三段结果的 UNION。

    输出列：kind(TABLE/COLUMN)、位置(库.表 或 表名)、对象名、备注。
    MySQL 单库模式（配置了 database）只搜该库，多库模式搜全部非系统库；
    达梦按默认模式搜索。
    """
    kw = like_pattern(f"%{keyword}%")
    lim = f"LIMIT {int(limit)}"
    if entry_db_type(entry) == DAMENG:
        owner = _dameng_owner(entry, None)
        owner_filt = _dameng_owner_filt(owner, "C")
        return (
            "SELECT * FROM ("
            f"SELECT 'TABLE' AS KIND, TABLE_NAME AS LOC, TABLE_NAME AS NAME, NULL AS REMARK "
            f"FROM ALL_TABLES WHERE 1=1{_dameng_owner_filt(owner, '')} "
            f"AND TABLE_NAME LIKE {kw} ESCAPE '\\' "
            "UNION ALL "
            "SELECT 'COLUMN' AS KIND, C.TABLE_NAME AS LOC, C.COLUMN_NAME AS NAME, M.COMMENTS AS REMARK "
            f"{_DM_COL_COMMENTS_JOIN} WHERE 1=1{owner_filt} "
            f"AND (C.COLUMN_NAME LIKE {kw} ESCAPE '\\' OR M.COMMENTS LIKE {kw} ESCAPE '\\') "
            f") {lim}"
        )
    scope = (
        f"TABLE_SCHEMA = {qstr(entry['database'])}"
        if entry.get("database")
        else ("TABLE_SCHEMA NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')")
    )
    return (
        "SELECT * FROM ("
        "SELECT 'TABLE' AS KIND, CONCAT(TABLE_SCHEMA, '.', TABLE_NAME) AS LOC, "
        "TABLE_NAME AS NAME, TABLE_COMMENT AS REMARK FROM information_schema.TABLES "
        f"WHERE {scope} AND TABLE_NAME LIKE {kw} "
        "UNION ALL "
        "SELECT 'COLUMN' AS KIND, CONCAT(TABLE_SCHEMA, '.', TABLE_NAME) AS LOC, "
        "COLUMN_NAME AS NAME, COLUMN_COMMENT AS REMARK FROM information_schema.COLUMNS "
        f"WHERE {scope} AND (COLUMN_NAME LIKE {kw} OR COLUMN_COMMENT LIKE {kw}) "
        f") {lim}"
    )


def table_sample_default(entry: dict) -> int:
    return 100


def schema_row_to_csv(row: tuple, entry: dict) -> tuple:
    """把元数据行统一为 CSV 输出（达梦 NULLABLE Y/N → YES/NO 与 MySQL 对齐）。"""
    if entry_db_type(entry) == DAMENG and len(row) >= 3 and row[2] in ("Y", "N"):
        row = (row[0], row[1], "YES" if row[2] == "Y" else "NO", *row[3:])
    return row


def _mysql_error_message(e: Exception) -> str:
    return getattr(e, "msg", None) or str(e) or "Unknown MySQL error"


def _dameng_error_message(e: Exception) -> str:
    return str(e) or "Unknown Dameng error"


def error_message(entry: dict, e: Exception) -> str:
    if entry_db_type(entry) == DAMENG:
        return _dameng_error_message(e)
    return _mysql_error_message(e)


def mysql_use_pure() -> bool:
    """MySQL 连接是否强制走纯 Python 实现（默认 True）。

    mysql-connector 26.7+ 的 C 扩展按 `_mysql_connector.__file__` 相对路径查找
    自带认证插件（mysql/vendor/plugin/*.so），PyInstaller 单文件冻结后该路径
    不存在，会回退到系统 /usr/lib/mysql/plugin——目标机通常没有，导致
    mysql_native_password 认证直接报 “plugin cannot be loaded”。
    纯 Python 路径的插件模块（mysql.connector.plugins.*）会被 --collect-all mysql
    一并收集，冻结后行为稳定。显式设置 MYSQL_USE_PURE=false 可恢复 C 扩展。
    """
    env = os.getenv("MYSQL_USE_PURE")
    if env is not None:
        return env.strip().lower() != "false"
    return True


def query_timeout_seconds(entry: dict) -> int:
    """语句超时（秒）：条目 query_timeout 优先，其次 MYSQL_MCP_QUERY_TIMEOUT，默认 60；0 表示不限。"""
    value = (entry or {}).get("query_timeout")
    if value is None:
        value = os.getenv("MYSQL_MCP_QUERY_TIMEOUT")
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = 60
    return max(0, timeout)


def build_config(entry: dict, role: str, host=None, port=None) -> dict:
    """从别名条目构造驱动原生连接参数；role='read'|'write' 选择对应账号。"""
    acct = entry[f"{role}_user"]
    if entry_db_type(entry) == DAMENG:
        cfg = {
            "user": acct["user"],
            "password": acct["password"],
            "server": host or entry["host"],
            "port": int(port or entry.get("port") or DEFAULT_PORTS[DAMENG]),
            "autoCommit": True,
            "login_timeout": int(entry.get("connect_timeout", 10)) * 1000,
        }
        if entry.get("database"):
            cfg["schema"] = entry["database"]
        return {k: v for k, v in cfg.items() if v is not None}
    cfg = {
        "host": host or entry["host"],
        "port": port or int(entry.get("port", 3306)),
        "user": acct["user"],
        "password": acct["password"],
        "charset": entry.get("charset") or "utf8mb4",
        "collation": entry.get("collation") or "utf8mb4_unicode_ci",
        "autocommit": True,
        "sql_mode": entry.get("sql_mode") or "TRADITIONAL",
        "connect_timeout": int(entry.get("connect_timeout", 10)),
        "use_pure": mysql_use_pure(),
    }
    timeout = query_timeout_seconds(entry)
    if timeout > 0:
        # 读超时兜底慢查询/挂起语句（MySQL 服务端 max_execution_time 只覆盖 SELECT，
        # 且需要额外会话设置；socket 级超时对所有语句生效且实现简单）。
        cfg["read_timeout"] = timeout
        cfg["write_timeout"] = timeout
    if entry.get("database"):
        cfg["database"] = entry["database"]
    return {k: v for k, v in cfg.items() if v is not None}


def _ensure_dm_ssl_path() -> None:
    """确保 dmPython 能找到加密模块（libssl）。

    官方要求 LD_LIBRARY_PATH 指向 dmPython 安装目录下的 dmssl 子目录；
    单文件打包（PyInstaller onefile）运行时该目录位于 sys._MEIPASS 下，
    在 import dmPython 前把两处候选路径都加入 LD_LIBRARY_PATH。
    """
    import os
    import sys

    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "dmssl"))
    try:
        import site
        for sp in site.getsitepackages() + [site.getusersitepackages()]:
            candidates.append(os.path.join(sp, "dmssl"))
    except Exception:
        pass
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    paths = [p for p in candidates if os.path.isdir(p) and p not in existing.split(":")]
    if paths:
        os.environ["LD_LIBRARY_PATH"] = ":".join([*paths, existing] if existing else paths)
        logger.debug("LD_LIBRARY_PATH extended with dmssl: %s", paths)


def connect_entry(entry: dict, role: str, host=None, port=None):
    """按条目类型建立 DB API 2.0 连接；调用方负责 close（支持 with）。"""
    if entry_db_type(entry) == DAMENG:
        _ensure_dm_ssl_path()
        try:
            import dmPython
        except ImportError:
            raise RuntimeError(
                "连接达梦数据库需要安装 dmPython 驱动：pip install dmPython"
            )
        return dmPython.connect(**build_config(entry, role, host, port))
    from mysql.connector import connect
    return connect(**build_config(entry, role, host, port))


def connect_error_class(entry: dict):
    """返回驱动的异常基类，用于执行路径的错误捕获。"""
    if entry_db_type(entry) == DAMENG:
        try:
            import dmPython
            return dmPython.Error
        except ImportError:
            return Exception
    from mysql.connector import Error
    return Error


def is_system_schema_dameng(name: str) -> bool:
    return name.upper() in _DM_SYSTEM_SCHEMAS
