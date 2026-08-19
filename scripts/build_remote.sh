#!/usr/bin/env bash
# 服务器端构建脚本：venv 隔离 + 安装依赖 + PyInstaller 打包
set -uo pipefail
cd /root/build_mcp || exit 1
export PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

echo "=== [1/4] venv ==="
python3 -m venv venv || exit 1
PY=/root/build_mcp/venv/bin/python

echo "=== [2/4] pip install (pinned mcp<2) ==="
$PY -m pip install --quiet --no-cache-dir --upgrade pip
$PY -m pip install --no-cache-dir "mcp>=1.2.0,<2" . pyinstaller
rc=$?
if [ $rc -ne 0 ]; then
  echo "RETRY with tsinghua mirror"
  $PY -m pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple "mcp>=1.2.0,<2" . pyinstaller || { echo "INSTALL_FAILED"; exit 1; }
fi
$PY -c "import mcp; print('mcp version:', mcp.__version__ if hasattr(mcp,'__version__') else 'n/a')"

echo "=== [3/4] pyinstaller build ==="
rm -rf build dist
# 不用 --collect-all mcp（会连带 mcp.cli 依赖 typer 而失败）；
# 只补齐 server.py 中延迟/条件 import 的模块
$PY -m PyInstaller --onefile --name mysql_mcp_server \
  --hidden-import mcp.server.sse \
  --hidden-import mcp.server.transport_security \
  --hidden-import mcp.server.lowlevel.server \
  --collect-data mysql_mcp_server \
  --paths src \
  --distpath dist \
  scripts/run_entry.py || { echo "BUILD_FAILED"; exit 1; }

cp scripts/start.sh scripts/stop.sh dist/
echo "=== [4/4] done ==="
ls -lh dist/
echo "ALL_OK"
