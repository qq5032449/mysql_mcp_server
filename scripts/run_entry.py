"""PyInstaller 打包入口（Linux 单文件构建用）。

不能直接把 src/mysql_mcp_server/__main__.py 作为 PyInstaller 入口
（其模块名 __main__ 会与包内相对解析冲突），故提供此独立入口。
"""

import asyncio

from mysql_mcp_server.server import main

if __name__ == "__main__":
    asyncio.run(main())
