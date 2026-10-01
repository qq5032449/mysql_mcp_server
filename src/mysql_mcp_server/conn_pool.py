"""MySQL 连接池：按连接配置指纹复用连接，消除每次工具调用的 TCP+认证开销。

设计：
- 指纹 = sha1(驱动完整连接参数)，host/port/账号/密码/库名任一变化即新池，
  旧池随引用消失自然废弃；哈希保证密码不会出现在池名/日志里。
- 池耗尽（PoolError）时降级为临时直连，不阻塞调用方。
- 池构造失败（连不上）时移除缓存并按原路径直连报错，行为与无池一致。
- 仅 MySQL 启用（dmPython 无池化能力）；达梦与 pool_size=0 走直连。
- close() 语义不变：池连接归还池，直连真正关闭，调用方代码无感知。
"""

import hashlib
import json
import logging
import os
import threading

from mysql_mcp_server import db_drivers

logger = logging.getLogger("mysql_mcp_server.conn_pool")

_pools: dict[str, object] = {}
_lock = threading.RLock()


def pool_size_for(entry: dict) -> int:
    """池大小：条目 pool_size 优先，其次 MYSQL_MCP_POOL_SIZE，默认 3；0 表示禁用池。"""
    value = (entry or {}).get("pool_size")
    if value is None:
        value = os.getenv("MYSQL_MCP_POOL_SIZE")
    try:
        size = int(value)
    except (TypeError, ValueError):
        size = 3
    return max(0, min(size, 32))  # 驱动上限 32


def _fingerprint(entry: dict, role: str, host, port) -> str:
    cfg = db_drivers.build_config(entry, role, host, port)
    payload = json.dumps(cfg, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def clear_all() -> None:
    """清空池缓存（测试用；已借出的连接不受影响）。"""
    with _lock:
        _pools.clear()


def get_connection(entry: dict, role: str, host=None, port=None):
    """获取连接：MySQL 且池启用时走池，否则与 connect_entry 相同的直连路径。"""
    if db_drivers.entry_db_type(entry) != db_drivers.MYSQL:
        return db_drivers.connect_entry(entry, role, host, port)
    size = pool_size_for(entry)
    if size <= 0:
        return db_drivers.connect_entry(entry, role, host, port)

    key = _fingerprint(entry, role, host, port)
    pool = _pools.get(key)
    if pool is None:
        with _lock:
            pool = _pools.get(key)
            if pool is None:
                from mysql.connector.pooling import MySQLConnectionPool
                pool = MySQLConnectionPool(
                    pool_name=f"mcp_{key}",
                    pool_size=size,
                    **db_drivers.build_config(entry, role, host, port),
                )
                _pools[key] = pool
    try:
        return pool.get_connection()
    except Exception as e:
        from mysql.connector.errors import PoolError
        if isinstance(e, PoolError):
            logger.debug("pool %s exhausted; falling back to direct connection", key)
            return db_drivers.connect_entry(entry, role, host, port)
        # 池内连接全部失效且重连失败等：丢弃池缓存，直连让错误按原样暴露
        with _lock:
            _pools.pop(key, None)
        raise
