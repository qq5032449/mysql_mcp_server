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
$PY -m pip install --no-cache-dir "mcp>=1.2.0,<2" ".[dameng]" pyinstaller staticx
rc=$?
if [ $rc -ne 0 ]; then
  echo "RETRY with tsinghua mirror"
  $PY -m pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple "mcp>=1.2.0,<2" ".[dameng]" pyinstaller || { echo "INSTALL_FAILED"; exit 1; }
fi
$PY -c "import mcp; print('mcp version:', mcp.__version__ if hasattr(mcp,'__version__') else 'n/a')"

# staticx 不接受带 DT_RUNPATH 的库（mysql-connector vendored .so 有此问题），
# 打包前先清理 venv 内全部 .so 的 RPATH/RUNPATH（staticx 产物运行时自行解析库路径）
command -v patchelf >/dev/null 2>&1 || {
  yum install -y patchelf >/dev/null 2>&1 || apt-get install -y patchelf >/dev/null 2>&1 || true
}
if command -v patchelf >/dev/null 2>&1; then
  find /root/build_mcp/venv -name "*.so*" -type f -exec patchelf --remove-rpath {} \; 2>/dev/null || true
  echo "rpath cleaned"
else
  echo "WARN: patchelf unavailable, staticx step may fail"
fi

echo "=== [3/4] pyinstaller build ==="
rm -rf build dist
# 不用 --collect-all mcp（会连带 mcp.cli 依赖 typer 而失败）；
# 只补齐 server.py 中延迟/条件 import 的模块
# --collect-all mysql：mysql-connector 的认证插件（plugins/mysql_native_password）
# 与错误消息（locales/eng）均为运行时动态加载，必须整体收集
# dmPython 是单 .so 模块（非包），--collect-all 不生效；用 --collect-binaries
# 收集其 .so 及 dmpython.libs/ 下的 DPI 库（libdmdpi），dmssl/ 下的加密库
SITE=$($PY -c "import site; print(site.getsitepackages()[0])")
$PY -m PyInstaller --onefile --name mysql_mcp_server \
  --hidden-import mcp.server.sse \
  --hidden-import mcp.server.transport_security \
  --hidden-import mcp.server.lowlevel.server \
  --collect-all mysql \
  --collect-binaries dmPython \
  --add-binary "$SITE/dmpython.libs/*.so:dmpython.libs" \
  --add-data "$SITE/dmssl:dmssl" \
  --collect-data mysql_mcp_server \
  --paths src \
  --distpath dist \
  scripts/run_entry.py || { echo "BUILD_FAILED"; exit 1; }

cp scripts/start.sh scripts/stop.sh dist/

echo "=== [4/4] staticx（静态化，摆脱 glibc 版本依赖）==="
# PyInstaller 产物依赖构建机的 glibc；staticx 把 glibc 打入二进制，
# 使其可在任意 x86_64 Linux（含 CentOS 7 等老 glibc 系统）运行
# staticx 默认用 /tmp 解包（小内存机器 tmpfs 易满），改到构建目录下
STATICX_FLAGS="--tempdir /root/build_mcp/staticx-tmp"
mkdir -p /root/build_mcp/staticx-tmp
command -v patchelf >/dev/null 2>&1 || {
  yum install -y patchelf >/dev/null 2>&1 || apt-get install -y patchelf >/dev/null 2>&1 || true
}
$PY -m staticx $STATICX_FLAGS dist/mysql_mcp_server dist/mysql_mcp_server.staticx \
  || { echo "STATICX_FAILED（保留非静态版本）"; }
if [ -f dist/mysql_mcp_server.staticx ]; then
  mv dist/mysql_mcp_server.staticx dist/mysql_mcp_server
  chmod +x dist/mysql_mcp_server
  file dist/mysql_mcp_server 2>/dev/null || true
  ldd dist/mysql_mcp_server 2>&1 | head -1 || true
fi

echo "=== done ==="
ls -lh dist/
echo "ALL_OK"
