# 多库模式 HTTP 服务镜像：SSE + Streamable HTTP + 管理页面
# 单库 stdio 用法仍可用：docker run -e MCP_TRANSPORT=stdio ...
FROM python:3.11-slim

WORKDIR /app

# 先装依赖再拷源码，利用层缓存（pyproject 已锁 mcp>=1.2.0,<2）
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[sse]"

# 需要达梦支持时改用：
#   RUN pip install --no-cache-dir ".[sse,dameng]"

# 配置（databases.json）与日志放在卷里，容器升级不丢
VOLUME ["/app/config", "/app/logs"]

ENV MCP_TRANSPORT=sse \
    MCP_SSE_HOST=0.0.0.0 \
    MCP_SSE_PORT=8000 \
    MYSQL_MCP_CONFIG_DIR=/app/config

EXPOSE 8000

# 健康检查：管理 API health（回环放行，无需令牌）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/admin/api/health', timeout=3).status==200 else 1)"

# 非 root 运行
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# NOTE: 不要把 MYSQL_PASSWORD / ADMIN_TOKEN 写进镜像。
# 运行时传入：
#   docker run -e ADMIN_TOKEN=... -v ./config:/app/config -p 8000:8000 镜像名
# 单机部署记得设 MCP_SSE_ALLOWED_HOSTS 或反向代理透传 Host。
CMD ["python", "-m", "mysql_mcp_server.server"]
