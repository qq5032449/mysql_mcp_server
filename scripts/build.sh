#!/usr/bin/env bash
# 在 Linux 服务器上构建 mysql_mcp_server 单文件可执行文件
# 用法：bash scripts/build.sh
# 要求：python3 >= 3.11（可用 PYTHON=python3.12 指定解释器）
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"

echo "==> 使用解释器: $($PYTHON --version)"
$PYTHON -m pip install --upgrade pip
$PYTHON -m pip install . pyinstaller

rm -rf build dist
$PYTHON -m PyInstaller \
    --onefile \
    --name mysql_mcp_server \
    --collect-all mcp \
    --collect-data mysql_mcp_server \
    --paths src \
    --distpath dist \
    scripts/run_entry.py

cp scripts/start.sh scripts/stop.sh dist/
echo ""
echo "==> 构建完成："
ls -lh dist/
echo ""
echo "部署：把 dist/ 下三个文件（mysql_mcp_server、start.sh、stop.sh）拷到目标目录，执行 ./start.sh"
