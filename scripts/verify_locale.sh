#!/usr/bin/env bash
set -uo pipefail
cd /opt/mysql_mcp || exit 1
MCP_SSE_PORT=15000 bash start.sh
sleep 3
echo "--- health ---"
curl -s -w "\nHTTP:%{http_code}\n" http://127.0.0.1:15000/admin/api/health
echo "--- create ---"
curl -s -w "\nHTTP:%{http_code}\n" -X POST http://127.0.0.1:15000/admin/api/databases -H 'Content-Type: application/json' -d '{"alias":"loctest","host":"127.0.0.1","port":3399,"read_user":{"user":"u","password":"p"},"write_user":{"user":"u","password":"p"}}'
echo "--- test ---"
curl -s -w "\nHTTP:%{http_code}\n" -X POST http://127.0.0.1:15000/admin/api/databases/loctest/test -H 'Content-Type: application/json' -d '{}'
curl -s -X DELETE http://127.0.0.1:15000/admin/api/databases/loctest > /dev/null
echo "--- server log tail ---"
tail -5 logs/server.log
bash stop.sh
