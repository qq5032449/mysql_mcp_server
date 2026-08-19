[![Tests](https://github.com/designcomputer/mysql_mcp_server/actions/workflows/test.yml/badge.svg)](https://github.com/designcomputer/mysql_mcp_server/actions)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/mysql-mcp-server)](https://pypi.org/project/mysql-mcp-server/)
[![AgentAudit Safe](https://img.shields.io/badge/AgentAudit-safe-brightgreen)](https://www.agentaudit.dev/packages/mysql-mcp-server)
# MySQL MCP Server
一个模型上下文协议（Model Context Protocol，MCP）实现，支持与 MySQL 数据库的安全交互。该服务端组件在 AI 应用（宿主/客户端）与 MySQL 数据库之间建立通信，通过受控接口让数据库的探索与分析更安全、更结构化。

> **注意**：MySQL MCP Server 同时支持标准输入输出（STDIO）与 Streamable HTTP（SSE）两种传输模式。远程/自托管部署推荐使用 SSE 模式。

## 部署方式
- **托管** — [Fronteir AI](https://fronteir.ai/mcp/designcomputer-mysql-mcp-server) 为你运行服务端，无需本地配置。
- **本地** — [Smithery](https://smithery.ai/server/designcomputer/mysql-mcp-server) 在你自己的机器上安装并运行服务端。

## 功能特性
- 以资源（resources）形式列出可用的 MySQL 表
- 读取表内容
- 执行 SQL 查询，并带有完善的错误处理
- **多数据库模式**（可选 `MYSQL_DATABASE`）
- **SSE/HTTP 传输支持**（`MCP_TRANSPORT=sse`）
- **SSH 隧道支持**
- **完整的表结构信息**
- **表数据采样**
- 通过环境变量安全地访问数据库
- 完善的日志记录

## 安装
### 手动安装
```bash
pip install mysql-mcp-server
```

### 通过 Smithery 安装
使用 [Smithery](https://smithery.ai/server/designcomputer/mysql-mcp-server) 为 Claude Desktop 自动安装 MySQL MCP Server：
```bash
npx -y @smithery/cli install designcomputer/mysql-mcp-server --client claude
```

### 通过 Claude Code CLI 安装
```bash
claude mcp add --transport stdio designcomputer-mysql_mcp_server uvx mysql_mcp_server
```

### 通过 Autohand Code CLI 安装
```bash
autohand mcp add mysql env MYSQL_HOST=localhost MYSQL_PORT=3306 MYSQL_USER=your_username MYSQL_PASSWORD=your_password MYSQL_DATABASE=your_database uvx mysql_mcp_server
```

在 `mcp add` 后加 `--scope project` 可将注册信息保留在当前工作区。当前 CLI 详情见 [Autohand Code](https://github.com/autohandai/code-cli/)。

## 配置
设置以下环境变量：
```bash
MYSQL_HOST=localhost     # 数据库主机
MYSQL_PORT=3306         # 可选：数据库端口（不指定时默认 3306）
MYSQL_USER=your_username
MYSQL_PASSWORD=your_password
MYSQL_DATABASE=your_database # 可选：留空则进入多数据库模式

# 高级配置
MYSQL_SSL_MODE=DISABLED  # DISABLED、REQUIRED、VERIFY_CA、VERIFY_IDENTITY
MYSQL_CONNECT_TIMEOUT=10 # 超时时间（秒）

# 连接行为（可选）
MYSQL_SQL_MODE=TRADITIONAL           # 连接所应用的 SQL mode（默认：TRADITIONAL）

# 兼容性（可选）
MYSQL_CHARSET=utf8mb4
MYSQL_COLLATION=utf8mb4_unicode_ci
MYSQL_AUTH_PLUGIN=       # 例如旧版 MySQL 使用 mysql_native_password
MYSQL_USE_PURE=false     # 强制使用纯 Python 连接器（默认：false）
MYSQL_RAISE_ON_WARNINGS=false        # 出现 SQL 警告时抛出异常（默认：false）

# SSE 传输（可选）
MCP_TRANSPORT=stdio      # stdio 或 sse
MCP_SSE_HOST=0.0.0.0     # 监听所有网卡（Docker/托管部署需要）
PORT=8000                # HTTP 端口（MCP_SSE_PORT 的回退值）
MCP_SSE_ALLOWED_HOSTS=   # 逗号分隔的允许 Host 头（默认：localhost:{port},127.0.0.1:{port}）

# SSH 隧道（可选）
MYSQL_SSH_ENABLE=false   # 设为 true 启用
MYSQL_SSH_HOST=          # SSH 跳板机
MYSQL_SSH_PORT=22        # SSH 端口
MYSQL_SSH_USER=          # SSH 用户名
MYSQL_SSH_KEY_PATH=      # SSH 私钥路径
MYSQL_SSH_REMOTE_HOST=localhost # 从跳板机视角看的目标主机
MYSQL_SSH_REMOTE_PORT=3306
MYSQL_LOCAL_PORT=3330
```

### `.env` 文件加载

服务端启动时通过 `python-dotenv` 自动加载 `.env` 文件，本地使用只需：

```bash
cp .env.example .env   # 然后填入你的凭据
```

该文件从**进程工作目录**（及其父目录）读取，在项目目录下自行启动服务端时可以正常生效。

> ⚠️ **Claude Code / Claude Desktop：** 这些宿主会从它们自己的工作目录启动服务端，因此**找不到**项目里的 `.env`，你会看到 `Missing required database configuration`。请把 `MYSQL_*` 的值写进 MCP 配置的 `env` 块（见下方"使用方式"），不要依赖 `.env`。

### 多数据库模式
未设置 `MYSQL_DATABASE` 时，服务端进入多数据库模式：
- `list_resources` 返回所有用户数据库（系统数据库会被过滤）
- 在 SQL 查询中使用全限定表名，如 `mydb.mytable`
- **注意：** 仅支持单条 SQL 语句，不支持多语句查询（如 `USE db; SELECT ...`）。

## 管理页面与多数据库别名（SSE 模式）

以 SSE 模式启动服务端，打开内置管理页面即可管理多个数据库连接，每个连接可配置**独立的读/写账号**：

```bash
# Windows PowerShell
$env:MCP_TRANSPORT="sse"; $env:MCP_SSE_PORT="8000"; python -m mysql_mcp_server
# Linux/macOS
MCP_TRANSPORT=sse MCP_SSE_PORT=8000 python -m mysql_mcp_server
```

管理页面：`http://127.0.0.1:8000/admin/`（仅限回环访问——管理 API 与页面会拒绝非回环客户端和未知 Host 头；不要将其置于反向代理之后）。

每个别名可配置：

| 字段 | 用途 |
|---|---|
| 连接（host/port/database） | 连接目标。database 留空即为多数据库模式。 |
| 查询用户（read_user） | 用于 SELECT / SHOW / DESCRIBE / EXPLAIN |
| 操作用户（write_user） | 用于**确认后**的 DML/DDL |
| write_policy | `client_confirm`（默认）：客户端不支持 elicitation 时，信任客户端自身的工具确认 UI 继续执行写操作。`elicitation_only`：客户端无法展示服务端确认弹窗时直接拒绝写操作。 |
| allow_delete | DELETE / TRUNCATE / DROP 的总开关（默认关闭） |

客户端按别名连接：`http://127.0.0.1:8000/sse?alias=db1`
（省略 `alias` 时使用默认别名）。当 `config/databases.json` 中没有任何条目时，原有的 `MYSQL_*` 环境变量仍可作为向后兼容的单数据库回退（该模式下读写共用同一账号）。

> 注意与上文[多数据库模式](#多数据库模式)的区别：那个模式是在**单个连接**上暴露多个 *schema*；而别名管理的是多个**连接**，每个连接有独立的账号与写策略。

**写操作如何确认：** 服务端对每条语句做三级判定（读 / 写 / 删除）。读操作直接用查询账号执行；写操作与删除操作会触发 MCP **elicitation** 弹窗展示完整 SQL——接受则用操作账号执行，拒绝则中止。客户端不支持 elicitation 时，按别名的 `write_policy` 决定降级行为（见上表）。所有写操作尝试都会记入管理页面的审计列表（磁盘上为 `logs/audit.log`）。

## 可用工具

### `execute_sql`
执行任意标准 SQL 查询。
- **参数：** `query`（字符串）
- **功能：** 支持 `SELECT`、`SHOW`、`DESCRIBE` 与 DML（`INSERT`、`UPDATE`、`DELETE`）。DML 操作带有破坏性提示标记。
- **限制：** 仅支持单条语句，不支持多语句查询。
- **跨库：** 无论 `MYSQL_DATABASE` 设置为何，都可用 `database.table` 写法查询任意数据库。

### `get_schema_info`
提供数据库结构的详细元数据。
- **参数：** `table_name`（可选字符串）
- **输出：** 列名、类型、可空性、默认值与注释。
- **跨库：** 传入 `database.table` 可查询 `MYSQL_DATABASE` 之外的库；裸表名使用已配置的数据库。
- **标识符规则：** 名称只能包含字母数字、下划线与 `$`（允许用一个点作为 `database.table` 分隔符）。

### `get_table_sample`
获取有代表性的数据样本。
- **参数：** `table_name`（字符串）、`limit`（可选整数，最大 20）
- **用途：** 无需拉取大结果集即可快速了解数据格式与内容。
- **跨库：** 传入 `database.table` 可采样 `MYSQL_DATABASE` 之外的库；裸表名使用已配置的数据库。
- **标识符规则：** 名称只能包含字母数字、下划线与 `$`（允许用一个点作为 `database.table` 分隔符）。

## 可用提示（Prompts）

除工具外，服务端还提供 **MCP prompts**——客户端可按需启动的引导式多步工作流。在 Claude Code 中以斜杠命令形式出现（`/mcp__<server>__<prompt>`）；在 Claude Desktop 中位于提示（`+`）菜单。

| Prompt | 参数 | 描述 |
| --- | --- | --- |
| `explore_database` | *（无）* | 系统性探索数据库：发现可用表、查看表结构、采样数据并总结内容。 |
| `analyze_table` | `table_name` *（必填）* | 深入分析指定表：获取表结构、采样数据并给出实用查询建议。支持 `database.table` 写法跨库查询。 |

**示例（Claude Code）：**
```
/mcp__mysql__explore_database
/mcp__mysql__analyze_table customers
```

两个提示均编排现有的 `get_schema_info` 与 `get_table_sample` 工具；`explore_database` 还会利用资源列表来枚举表。

## 使用方式
### 配合 Claude Desktop
将以下内容加入 `claude_desktop_config.json`：
```json
{
  "mcpServers": {
    "mysql": {
      "command": "uv",
      "args": [
        "--directory",
        "path/to/mysql_mcp_server",
        "run",
        "mysql_mcp_server"
      ],
      "env": {
        "MYSQL_HOST": "localhost",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "your_username",
        "MYSQL_PASSWORD": "your_password",
        "MYSQL_DATABASE": "your_database"
      }
    }
  }
}
```

更详细的示例与各 Agent 专属指引见 [MCP_USECASES.md](MCP_USECASES.md)。

### 配合 Visual Studio Code
将以下内容加入 `mcp.json`：
```json
{
  "mcpServers": {
    "mysql": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "mysql-mcp-server",
        "mysql_mcp_server"
      ],
      "env": {
        "MYSQL_HOST": "localhost",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "your_username",
        "MYSQL_PASSWORD": "your_password",
        "MYSQL_DATABASE": "your_database"
      }
    }
  }
}
```
注意：需要先安装 uv。

### 使用 MCP Inspector 调试
MySQL MCP Server 并非设计为独立运行或直接用 Python 命令行启动的程序，但你可以使用 MCP Inspector 进行调试。

MCP Inspector 为测试和调试 MCP 实现提供了便捷方式：

```bash
# 安装依赖
pip install -r requirements.txt
# 使用 MCP Inspector 调试（不要直接用 Python 运行）
```

MySQL MCP Server 设计为集成到 Claude Desktop 等 AI 应用中，不应作为独立 Python 程序直接运行。

## 开发
```bash
# 克隆仓库
git clone https://github.com/designcomputer/mysql_mcp_server.git
cd mysql_mcp_server
# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Windows 上用 `venv\Scripts\activate`
# 安装开发依赖
pip install -r requirements-dev.txt
# 复制示例配置并填入你的凭据
cp .env.example .env
# 编辑 .env，填入 MySQL 连接信息
# 运行测试
pytest
```

## 安全注意事项
- **标识符校验：** 传给 `get_schema_info` 与 `get_table_sample` 的表名和库名会经过严格白名单校验（仅允许字母数字、下划线与 `$`；允许一个点作为 `database.table` 分隔符）。其他特殊字符一律拒绝，以防止 SQL 注入。
- **加密访问：** 全面支持 SSL/TLS 与 SSH 隧道，保障远程连接安全。
- **日志隐私：** 密码与 SSH 私钥在服务端日志中自动脱敏。
- **最小权限：** 始终使用权限最小化的专用 MySQL 用户。
- **SSE 传输没有内置认证。** SSE 服务端默认绑定 `0.0.0.0` 并接受无凭据连接。如果暴露到 localhost 之外，请放在强制认证的反向代理（nginx、Caddy、Traefik）后面。nginx + HTTP Basic Auth 示例：

  ```nginx
  location /sse {
      auth_basic "MCP";
      auth_basic_user_file /etc/nginx/.htpasswd;
      proxy_pass http://127.0.0.1:8000;
      proxy_set_header Host $host;
      proxy_buffering off;
  }
  location /messages/ {
      auth_basic "MCP";
      auth_basic_user_file /etc/nginx/.htpasswd;
      proxy_pass http://127.0.0.1:8000;
      proxy_set_header Host $host;
  }
  ```

  设置 `MCP_SSE_HOST=127.0.0.1` 使服务端只监听回环地址，代理成为唯一公网入口。将 `MCP_SSE_ALLOWED_HOSTS` 设为代理转发的公网主机名（例如 `MCP_SSE_ALLOWED_HOSTS=myserver.example.com:443`）。

部署安全完整指南见 [SECURITY.md](SECURITY.md)。

## 安全最佳实践
本 MCP 实现需要数据库访问权限才能工作。为了安全：
1. **创建专用 MySQL 用户**并授予最小权限
2. **绝不使用 root 凭据**或管理员账号
3. **限制数据库访问**至必要操作
4. **启用日志**用于审计
5. **定期安全审查**数据库访问

详细操作说明见 [MySQL 安全配置指南](https://github.com/designcomputer/mysql_mcp_server/blob/main/SECURITY.md)，包括：
- 创建受限 MySQL 用户
- 设置合适的权限
- 监控数据库访问
- 安全最佳实践

⚠️ 重要：配置数据库访问时务必遵循最小权限原则。

## 许可证
MIT License - 详情见 LICENSE 文件。

## 参与贡献
1. Fork 本仓库
2. 创建功能分支（`git checkout -b feature/amazing-feature`）
3. 提交变更（`git commit -m 'Add some amazing feature'`）
4. 推送分支（`git push origin feature/amazing-feature`）
5. 发起 Pull Request
