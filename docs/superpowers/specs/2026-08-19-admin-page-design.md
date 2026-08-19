# MySQL MCP Server 管理页面与双账号安全执行 设计文档

日期：2026-08-19
状态：已获用户批准的设计（待实现）

## 1. 背景与目标

当前 mysql_mcp_server 通过 `.env` 环境变量配置单个数据库连接，只支持一套账号密码，无法：
- 管理多个数据库连接
- 区分"查询账号"与"操作账号"实现最小权限
- 在执行危险 SQL 前由人工确认

本设计新增：
1. **Web 管理页面**：配置多个数据库连接（别名、连接信息、读账号、写账号、权限策略）
2. **别名路由**：MCP 客户端通过 SSE URL 参数（`?alias=db1`）选择数据库
3. **双账号执行**：读操作默认用只读账号；写操作经确认后用操作账号执行
4. **双通道写确认**：优先 MCP elicitation（服务端弹确认框），客户端不支持时按策略降级为客户端工具确认（`destructiveHint`）
5. **删除权限开关**：第三级控制，DELETE/TRUNCATE/DROP 需显式开启才可执行

## 2. 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| 运行模式 | SSE 单进程（管理页面与 MCP 端点同一 HTTP 服务） |
| 配置存储 | `config/databases.json` 明文存储 |
| 管理页面访问控制 | 仅绑定 127.0.0.1，无登录 |
| 写确认机制 | elicitation 优先 + 客户端工具确认降级（双通道） |
| 删除权限 | 按别名独立开关 `allow_delete`，默认关闭 |

## 3. 架构与别名路由

### 3.1 整体架构

```
┌─────────────┐   SSE /sse?alias=db1   ┌────────────────────────────────┐
│ MCP 客户端   │ ◄───────────────────► │  mysql_mcp_server (单进程)       │
│ (TRAE 等)   │                        │                                │
└─────────────┘                        │  ┌──────────────────────────┐  │
                                       │  │ MCP 核心（现有功能改造）    │  │
┌─────────────┐   HTTP /admin          │  │ · 按别名路由连接配置        │  │
│   浏览器     │ ◄───────────────────► │  │ · 读操作 → 只读账号直连     │  │
└─────────────┘   /api/*               │  │ · 写操作 → 确认→写账号执行  │  │
                                       │  └──────────────────────────┘  │
                                       │  ┌──────────────────────────┐  │
                                       │  │ 管理页面（Starlette 路由） │  │
                                       │  └──────────────────────────┘  │
                                       │  config/databases.json          │
                                       └────────────────────────────────┘
```

### 3.2 别名路由机制

- 客户端 MCP 配置写 SSE URL：`http://127.0.0.1:8000/sse?alias=db1`
- 服务端为每个别名维护独立的 Server 实例（懒创建，连接间完全隔离）
- 不带 `alias` 参数 → 使用 `default_alias`；无任何配置时返回明确错误提示
- 管理页面保存配置后重建实例缓存，新连接即生效（已连接会话保持旧配置直至重连）

### 3.3 配置文件 `config/databases.json`

```json
{
  "default_alias": "db1",
  "databases": {
    "db1": {
      "host": "localhost",
      "port": 3306,
      "database": "mydb",
      "charset": "utf8mb4",
      "read_user": { "user": "reader", "password": "pass1" },
      "write_user": { "user": "writer", "password": "pass2" },
      "write_policy": "client_confirm",
      "allow_delete": false,
      "ssh": { "enable": false }
    }
  }
}
```

- 复用现有 `MYSQL_*` 环境变量逻辑：环境变量存在时作为单库配置的向后兼容来源（未配置 JSON 时仍可用原方式运行）
- SSH 隧道配置按别名纳入（可选字段，沿用现有 MYSQL_SSH_* 字段集）

## 4. SQL 三级判定与双通道确认

### 4.1 关键字分类

```python
READ_PREFIXES = {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
# WITH (CTE) 不在此列，由 4.2 的 CTE 专用规则先处理，未匹配到主语句关键字时按默认写处理
WRITE_PREFIXES = {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP",
                  "TRUNCATE", "RENAME", "GRANT", "REVOKE", "SET", "LOCK", "UNLOCK",
                  "CALL", "LOAD", "OPTIMIZE", "ANALYZE", "REPAIR", "FLUSH", "RESET", "KILL"}
DELETE_PREFIXES = {"DELETE", "TRUNCATE", "DROP"}
```

### 4.2 判定规则

- 取去掉注释、空白后的**首个关键字**判定
- `SELECT ... FOR UPDATE` 按读处理（读账号可执行锁定读则执行，无权限时返回 MySQL 错误，可接受）
- **WITH (CTE)**：扫描括号深度为 0 的末尾主语句关键字（`WITH ... SELECT` → 读；`WITH ... INSERT/UPDATE/DELETE` → 写/删除；`ANALYZE TABLE` 按写处理，因前缀匹配无法与 `ANALYZE` 区分且其会更新索引统计）
- **默认写**：无法判定的一律按写处理（安全兜底）
- 多语句维持现有禁止逻辑不变

### 4.3 执行流程

```
execute_sql(query)
      │
      ▼
  三级判定（取首关键字 + CTE 主语句）
      │
      ├─ 读(SELECT/SHOW/...) ──► read_user 直接执行
      │
      ├─ 删除类(DELETE/TRUNCATE/DROP)
      │       ├─ allow_delete=false ──► 直接拒绝："该别名未开启删除权限，
      │       │                        请在管理页面开启后重试"
      │       └─ allow_delete=true ──► 走写确认流程
      │
      └─ 写(INSERT/UPDATE/CREATE/...) ──► 走写确认流程

写确认流程：
  尝试 elicitation ──支持──► 客户端弹出：别名/目标库、完整 SQL + 类型、确认/拒绝
      │                          ├─ accept ──► write_user 执行，返回受影响行数
      │                          └─ decline ─► 返回"用户已拒绝执行该 SQL"
      │
   不支持/超时(30s)
      ▼
  按别名的 write_policy 降级：
  · "client_confirm"（默认）─► 直接执行（信任客户端已通过 destructiveHint 确认 UI 放行）
  · "elicitation_only"     ─► 拒绝写操作，提示客户端不支持
```

注：`ALTER TABLE ... DROP COLUMN` 属表结构修改，归为普通写操作，不受删除开关限制。

### 4.4 关键细节

1. **elicitation 能力检测**：检查客户端 `initialize` 响应的 `capabilities.elicitation`（MCP 2025-06-18 规范）；Python SDK 通过 `request_context.request_elicitation()` 发起
2. **工具注解配合降级**：`execute_sql` 保持 `readOnlyHint=false, destructiveHint=true`，主流客户端调用前自身会弹确认框，作为降级防线
3. **write_policy 按别名配置**：存于 databases.json，管理页面可切换
4. **写执行连接**：write_user 连接 `autocommit=True`（沿用现状），执行后返回受影响行数；错误信息原样返回
5. **审计日志**：每次写操作记录 `时间 | 别名 | 确认通道(elicitation/client) | SQL 摘要`，删除类单独标记；内存最近 100 条 + `logs/audit.log` 文件

## 5. 管理页面 UI 与 API

### 5.1 页面结构（单页，纯 HTML+JS，无构建工具）

- **数据库列表卡片**：别名（默认标识）、主机:端口/库名、读/写用户名、写策略、删除权限状态（开启时红色醒目标识）、操作按钮（编辑/测试连接/删除）
- **客户端接入说明**：每个别名的 SSE URL（点击复制）+ TRAE/Claude 配置 JSON 示例
- **写操作审计**：最近记录列表（时间、别名、SQL 类型、确认通道、SQL 摘要）

### 5.2 编辑表单字段

| 分组 | 字段 |
|---|---|
| 基本信息 | 别名*（唯一标识）、主机、端口、数据库名、字符集（默认 utf8mb4） |
| 查询用户 | 用户名*、密码*（用于 SELECT/SHOW 等读操作） |
| 操作用户 | 用户名*、密码*（用于 DML/DDL 写操作） |
| 权限策略 | 写确认策略（client_confirm / elicitation_only）、删除权限开关 |
| 高级（折叠） | SQL mode、连接超时、SSH 隧道配置（沿用现有 MYSQL_SSH_* 字段集） |

### 5.3 API 设计（均为 JSON）

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/databases` | 列出所有配置（密码脱敏为 `****`） |
| POST | `/api/databases` | 新增（服务端校验别名唯一、必填项） |
| PUT | `/api/databases/{alias}` | 更新（密码为空表示不修改） |
| DELETE | `/api/databases/{alias}` | 删除（default_alias 被删时自动切换到剩余首个，无剩余则置空） |
| POST | `/api/databases/{alias}/test` | 用读账号试连（可选 `as_write=true` 测写账号），返回成功/错误详情 |
| PUT | `/api/settings` | 设置 default_alias |
| GET | `/api/audit` | 查询审计记录 |
| GET | `/api/health` | 服务状态 + 各配置数量 |

### 5.4 实现要点

1. **路由挂载**：在现有 SSE Starlette app 上追加 `Mount("/admin", ...)` 静态页 + `/api/*` 路由，单进程复用
2. **配置读写**：内存缓存 + 原子写 JSON（先写临时文件再 `os.replace`），保存后清除该别名的 Server 实例缓存
3. **前端**：`static/index.html` 单文件（内联 CSS/JS），fetch 调 API
4. **安全边界**：API 强制校验请求来源为回环地址（127.0.0.1/::1）
5. **审计存储**：内存 deque(100) + 每条同步追加 `logs/audit.log`

## 6. 测试策略

- **单元测试**：三级 SQL 判定（含 CTE、注释、边界情况）、配置 CRUD、密码脱敏
- **API 集成测试**：Starlette TestClient 测完整增删改查 + 测试连接（mock mysql connect）
- 新增 `tests/test_classify.py`、`tests/test_admin_api.py`，沿用现有 pytest 体系
