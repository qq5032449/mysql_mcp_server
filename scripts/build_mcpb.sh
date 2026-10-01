#!/usr/bin/env bash
# 打包 mysql_mcp_server 为 MCP Bundle（.mcpb，可双击/导入安装到 MCP 客户端）。
#
# 包类型：uv runtime —— 包内只含源码 + pyproject.toml，依赖由客户端装，
# 体积小且跨平台；stdio 传输；单库模式（安装界面填连接信息）。
#
# 用法：bash scripts/build_mcpb.sh [--no-install-test]
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# Git Bash 下 python 是 Windows 原生版：涉及 python/npx 的路径用 Windows 风格
if command -v cygpath >/dev/null 2>&1; then
  WIN_APP_DIR="$(cygpath -m "$APP_DIR")"
else
  WIN_APP_DIR="$APP_DIR"
fi
STAGE="$APP_DIR/dist/mcpb-stage"
OUT="$APP_DIR/dist/mysql-mcp-server.mcpb"

VERSION="$(python -c "import tomllib; print(tomllib.load(open(r'$WIN_APP_DIR/pyproject.toml','rb'))['project']['version'])")"
MANIFEST_VER="$(python -c "import json; print(json.load(open(r'$WIN_APP_DIR/mcpb/manifest.json'))['version'])")"
if [ "$VERSION" != "$MANIFEST_VER" ]; then
  echo "错误：pyproject.toml ($VERSION) 与 mcpb/manifest.json ($MANIFEST_VER) 版本不一致" >&2
  exit 1
fi

rm -rf "$STAGE"
mkdir -p "$STAGE"

# 源码与运行必需文件（排除测试、打包物、本地密钥与日志）
cp -r "$APP_DIR/src" "$STAGE/src"
cp "$APP_DIR/mcpb/server.py" "$STAGE/server.py"
cp "$APP_DIR/mcpb/manifest.json" "$STAGE/manifest.json"
cp "$APP_DIR/pyproject.toml" "$STAGE/pyproject.toml"
cp "$APP_DIR/README.md" "$STAGE/README.md"
mkdir -p "$STAGE/config"
find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name "*.pyc" -delete

cat > "$STAGE/.mcpbignore" << 'EOF'
.venv/
__pycache__/
*.pyc
.pytest_cache/
*.egg-info/
uv.lock
config/
logs/
EOF

mkdir -p "$APP_DIR/dist"
if command -v mcpb >/dev/null 2>&1; then
  mcpb pack "$STAGE" "$OUT"
else
  npx -y @anthropic-ai/mcpb pack "$STAGE" "$OUT"
fi

echo "已生成：$OUT"
unzip -l "$OUT"

# 安装前自检：解包到临时目录，用本地 python 跑 stdio 初始化握手
if [ "${1:-}" != "--no-install-test" ]; then
  TESTDIR="$(mktemp -d)"
  WIN_TESTDIR="$(command -v cygpath >/dev/null 2>&1 && cygpath -m "$TESTDIR" || echo "$TESTDIR")"
  trap 'rm -rf "$TESTDIR"' EXIT
  unzip -q "$OUT" -d "$TESTDIR"
  if [ -z "${MYSQL_USER:-}" ] || [ -z "${MYSQL_PASSWORD:-}" ]; then
    echo "跳过 stdio 自检（未设置 MYSQL_USER/MYSQL_PASSWORD 环境变量）"
  else
    printf '%s' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"build-check","version":"0"}}}' \
      | timeout 60 python "$WIN_TESTDIR/server.py" > "$TESTDIR/resp.txt" 2> "$TESTDIR/err.txt" || true
    if grep -q '"serverInfo"' "$TESTDIR/resp.txt" 2>/dev/null; then
      echo "stdio 自检通过：服务端正常响应 initialize"
    else
      echo "stdio 自检失败，stderr：" >&2
      tail -n 20 "$TESTDIR/err.txt" >&2 || true
      exit 1
    fi
  fi
fi
