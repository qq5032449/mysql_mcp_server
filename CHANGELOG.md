# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.1] - 2026-10-14

基于生产 `error_log.db`（75 条、2026-09-14~10-10）的分析结果，修复错误处理与配置校验上的四处短板。

### Fixed
- **达梦列注释查询报错（我方 bug）：**`get_schema_info` / `search_schema` 原先从 `ALL_TAB_COLUMNS` 直接取 `COMMENTS`，但部分达梦版本该视图没有此列（生产 `rpms-dm` 实测报 `Invalid column name [COMMENTS]`）。现改为 `LEFT JOIN ALL_COL_COMMENTS` 取注释；若该视图也不存在，`get_schema_info` 会自动降级重试一次不含注释的查询（`schema_sql_without_comments`），不会整体失败。

### Added
- **「列不存在」自动附真实列清单：**`Unknown column 'x'` / 达梦 `Invalid column name [x]` 时，从 SQL 中提取涉及的表（FROM/JOIN/UPDATE/INTO，最多 3 张）查出真实列名附在错误后，AI 无需再问一轮即可自纠。生产日志中此类错误占 49%，是收益最大的一项。查询失败不影响主流程，原始错误始终保留。
- **语法错误附标识符引用符提示：**MySQL 与达梦的语法错误都会附上「库名/表名含连字符、中文或与保留字同名时需加引用符」的提示（MySQL 反引号、达梦双引号），对应生产里 `SHOW COLUMNS FROM medical-my.consultation` 一类失败。
- **保存数据库时自动校验（不阻断）：**新建/修改数据库条目后自动做一次连通性与库存在性校验（复用「测试连接」逻辑），未通过时在响应 `warning` 字段与管理页面弹窗提示，保存本身照常生效——库暂时不可达或先建配置后建库都仍可用。`MYSQL_MCP_VERIFY_ON_SAVE=false` 可关闭。

### Changed
- `admin_api` 抽出 `probe_entry()` 供「测试连接」与保存校验共用；校验前会先展开用户引用，避免引用模式下探测拿到空账号。
- 达梦方言的列元数据查询统一使用表别名（`C.` / `M.`），`_dameng_owner_filt()` 支持不带别名前缀（此前传空别名会拼出 `UPPER(.OWNER)` 的错误 SQL）。

## [0.6.0] - 2026-10-01

### Added
- **用户维护功能：**新增顶层 `users` 用户表（名称即唯一键，含账号/密码），数据库条目通过 `read_user_ref`/`write_user_ref` 引用用户，
  连接数据库时使用被引用用户的账号密码。管理页面新增「用户管理」卡片（表格 + 添加/编辑/删除 + 迁移内联账号），
  数据库表单的用户名/密码输入改为下拉选择用户（读写账号可分别指定，也可选同一用户）。
- **用户管理 API：**`GET/POST /api/users`、`PUT/DELETE /api/users/{name}`、`POST /api/users/migrate`；
  列表与被引用情况脱敏返回，非回环访问同样受 ADMIN_TOKEN 保护。
- **改名级联：**修改用户名称时，后端自动同步所有数据库的引用，引用永不悬空。
- **删除保护：**用户正被数据库引用时拒绝删除（409 + 引用列表）；`DELETE ?force=true` 可强制删除并清空引用（管理页面二次确认）。
- **一键迁移：**`POST /api/users/migrate` 与页面按钮把仍使用内联账号的数据库迁成用户引用（按账号名去重，读写同账号合并）。
- **悬空引用提示：**引用用户被手工删改后，工具调用立即返回「数据库引用的用户不存在」的中文指引，而非连接报认证失败。

### Changed
- **配置结构：**`databases.json` 新增顶层 `users` 键；`masked_config` 同步脱敏用户密码；`/api/health` 增加 `users` 计数。
  旧版本配置无 `users` 键时自动补空表，零迁移；数据库内联账号（`read_user`/`write_user`）与 `.env` 单库回退模式继续可用。
- `validate_entry` 增加 `cfg` 参数并按引用校验；导入配置时同步校验 users 与引用完整性。
- 管理页「测试连接」在引用模式下先展开用户账号，悬空引用直接返回可操作提示。

## [0.5.0] - 2026-10-01

### Added
- **Streamable HTTP 传输：**新增 `POST /mcp?alias=xxx`（stateless + JSON 响应），无需 SSE 会话握手，供支持新协议的客户端使用；SSE 保持不变。每别名惰性启动会话管理器，与 SSE 共用别名路由与工具实现。
- **`search_schema` 工具：**按关键字搜索表名/列名/注释（`information_schema` / 达梦数据字典），大库上定位表远比全量拉取 schema 省时省 token；`limit` 默认 20、上限 50，LIKE 通配符已转义。
- **SELECT 行数上限：**结果集默认最多返回 500 行（`MYSQL_MCP_MAX_ROWS`，0 不限），超限附中文截断提示，SHOW TABLES 同样受限——防止 AI 跑无 LIMIT 的大查询打爆上下文。
- **语句超时：**MySQL 连接默认 60 秒 socket 读写超时（每别名 `query_timeout` 或 `MYSQL_MCP_QUERY_TIMEOUT`，0 不限），慢查询不再占死 worker 线程。
- **MySQL 连接池：**按连接配置指纹（含账号/密码/库名）复用连接，默认池大小 3（每别名 `pool_size` 或 `MYSQL_MCP_POOL_SIZE`，0 禁用）；池耗尽自动降级直连，达梦仍走直连。
- **SSH 隧道常驻化：**隧道按 ssh 配置指纹复用、本地端口自动分配（`local_port` 字段保留但不再使用），就绪探测替代 `sleep(2)`；管理页「测试连接」也走隧道（此前会绕过隧道直连，属修复）。
- **DDL 权限开关 `allow_ddl`（行为变化）：**CREATE/ALTER/RENAME/GRANT/REVOKE 现在需要别名开启「允许 DDL」才可执行（默认关）；DELETE/TRUNCATE/DROP 仍由 `allow_delete` 管控，行为不变。旧配置升级后 DDL 默认被拒，需在管理页面按需开启。
- **全表写警告：**UPDATE/DELETE 顶层无 WHERE 时，确认文案（弹窗与令牌消息）附加「将影响全表所有行」警示。
- **配置导出/导入：**`GET /api/databases/export`（含密码，令牌/回环保护）与 `POST /api/databases/import`（整份校验后原子替换），管理页面新增导出/导入按钮，支持备份迁移。
- **一键体检：**管理页面「一键体检」并发测试全部别名的读账号连通性并汇总结果。
- **错误日志筛选：**`GET /api/errors` 的 `alias`/`limit` 参数接入管理页面（别名过滤 + 50/100/500 条）。
- **审计增强：**审计记录新增执行状态（executed/error）、耗时、影响行数；`MYSQL_MCP_AUDIT_READ=true` 可选记录读操作；audit.log 超 5MB 自动轮转（保留一代）。
- **错误日志保留期：**`mcp_error_log` 每自然日惰性清理超过保留期的记录（默认 90 天，`MYSQL_MCP_ERROR_RETENTION_DAYS`，0 永久）。
- **Dockerfile 重写：**面向多库 HTTP 模式（SSE/Streamable HTTP/admin），config/logs 卷、HEALTHCHECK、非 root；单库 stdio 仍可用 `MCP_TRANSPORT=stdio`。

### Fixed
- **多语句误判：**`INSERT ... VALUES ('a;b')` 等字符串字面量/注释中的分号不再被误判为多语句（引号感知的顶层分号扫描替代朴素 `";" in query`）。
- **Windows zip 产物名：**`windos-amd64.zip` 更名为 `windows-amd64.zip`。

### Changed
- 审计内存上限 100 → 200 条；审计文件行新增耗时与行数两列。

## [0.4.8] - 2026-09-21

### Fixed
- **单文件部署认证插件报错：**`mysql-connector` 26.7+ 的 C 扩展按 `_mysql_connector.__file__` 相对路径查找自带认证插件（`mysql/vendor/plugin/*.so`），PyInstaller 单文件冻结后该路径不存在，回退到的系统 `/usr/lib/mysql/plugin` 通常也不存在，`mysql_native_password` 认证报 “Authentication plugin cannot be loaded”。现在 MySQL 连接默认强制纯 Python 实现（`use_pure=True`，插件模块随 `--collect-all mysql` 一并打包）；设置 `MYSQL_USE_PURE=false` 可恢复 C 扩展。管理页「测试连接」同步复用该配置（并保持不带库名探测）。

## [0.4.7] - 2026-09-11

### Added
- **测试连接校验库/模式存在性：**管理页面「测试连接」现在会在验证实例连通与账号认证之后，进一步校验配置的数据库名（达梦为模式名）是否存在且对账号可见，错误信息可区分「实例/账号不通」与「库不存在或无权访问」。多数据库模式（库名留空）跳过该校验。

## [0.4.6] - 2026-09-11

### Fixed
- **未初始化会话容错：**部分客户端（如 TRAE）SSE 重连后不在新会话上重走 `initialize` 握手直接发请求，SDK 以"未完成初始化"拒绝，客户端表现为 `Invalid request parameters`。现对跳过握手的会话自动补初始化并放行（记录 WARNING 日志）。安全性不受影响：此类会话无能力声明，写操作仍按 write_policy 降级走令牌二次确认，删除类仍受 allow_delete 约束。

## [0.4.5] - 2026-09-11

### Added
- **执行错误入库：**新增 `mcp_error_log` 表（本机 SQLite `logs/error_log.db`），记录真正打到数据库的执行失败（含别名、工具名、SQL、数据库返回错误）。确认流程拦截与 UPDATE/DELETE 无权限错误（`command denied`）不入库。管理页面新增「执行错误日志」表格与 `GET /admin/api/errors` 查询接口。
- **Dameng DM8 Support:** each database entry now carries a `db_type` (`mysql`/`dameng`, defaults to `mysql` so existing configs migrate with zero changes). Dameng entries connect via `dmPython` (optional `dameng` extra: `pip install "mysql_mcp_server[dameng]"`), with DM-dialect metadata queries (`USER_TABLES`/`ALL_TABLES`/`ALL_TAB_COLUMNS`), double-quote identifiers, default port 5236, and `database` mapped to schema. All three tools, confirmation flow, audit, and alias/project matching work identically across both types. Admin UI gains a database-type selector. Single-file build (`build_remote.sh`) collects dmPython runtime libraries.
- **MCP Bundle 打包：**新增 `mcpb/manifest.json`（`uv` 类型，manifest 0.4）与 `scripts/build_mcpb.sh` 一键打包脚本，产物 `dist/mysql-mcp-server.mcpb` 可导入 MCP 客户端安装（stdio 单库模式，安装界面配置连接信息）。

### Fixed
- **工具调用参数兼容：**关闭 SDK 的 jsonschema 预校验（`validate_input=False`），部分客户端把 `arguments` 发成 JSON 字符串、`limit` 发成字符串时不再报 `Invalid request parameters`/`Input validation error`，改为服务端宽松兼容并返回中文提示；`get_table_sample` 缺 `table_name` 时返回中文指引而非内部错误。

## [Unreleased]

## [0.4.4] - 2026-07-30

### Fixed
- **C Extension Fallback:** Stopped forcing `use_pure=False` in the connection config by default. `mysql-connector-python` treats an explicit `use_pure=False` as a hard requirement for its C extension, raising `ImportError` instead of falling back to the pure-Python implementation when the extension can't load (e.g. built against a newer OpenSSL than the host provides, as with `mysql-connector-python` 26.7.0 on many Linux systems). `use_pure` is now only set when `MYSQL_USE_PURE` is explicitly configured.

## [0.4.3] - 2026-07-30

### Fixed
- **Missing Runtime Dependency:** `python-dotenv` is now declared as a required dependency. The server imports it directly, but `uvx`'s isolated environment doesn't install it automatically, causing a `ModuleNotFoundError` on startup (#95).
- **MCP 2.x Incompatibility:** Constrained the `mcp` dependency to `<2` since MCP 2.0 introduced breaking API changes this server has not been migrated to support, and the previous unbounded `>=1.2.0` allowed `uvx` to resolve an incompatible version (#95).

## [0.4.1] - 2026-06-08

### Fixed
- **Package Metadata:** Split author entry into name-only (`Author:`) + maintainer-with-email (`Maintainer-email:`) so sites that read the legacy `Author` field (e.g. pypistats.org) display the author name correctly.

## [0.4.0] - 2026-06-08

### Added
- **Cross-Database Support:** `get_schema_info` and `get_table_sample` now accept `database.table` notation, making their scope consistent with `execute_sql`. Bare table names continue to use `MYSQL_DATABASE`.
- **MCP Prompts:** Two guided workflow prompts usable as slash commands in supporting clients (e.g. Claude Desktop):
  - `explore_database` — walks through resource discovery, schema inspection, data sampling, and summarization.
  - `analyze_table` — schema + sample + query suggestions for a named table.
- **Package Metadata:** Added `Homepage`, `Repository`, `Issues`, and `Changelog` URLs, SPDX license expression, keywords, and classifiers to PyPI metadata.
- **Reproducible Builds:** Committed `uv.lock` so hosted build environments get pinned dependencies.

### Fixed
- **Multi-Statement Error:** `execute_sql` now returns a clear message ("Only single statements are supported…") instead of MySQL's cryptic "Commands out of sync" error when a multi-statement query is passed.

### Changed
- **Tool Descriptions:** All three tools have richer descriptions that say when to use them and what they return. Contributor credits moved out of tool descriptions.
- **Tool Annotations:** `get_schema_info` and `get_table_sample` now carry `readOnlyHint=True` so clients can distinguish them from destructive operations.

## [0.3.1] - 2026-05-31

### Fixed
- **Strict LLM Compatibility:** Refactored resource names to be 'identifier-safe' (e.g., `table_users` instead of `Table: users`) to ensure compatibility with Google Gemini models and GitHub Copilot (Issue #39).
- **MySQL 5.7 Stability:** Added built-in support for `MYSQL_AUTH_PLUGIN`, `MYSQL_USE_PURE`, and `MYSQL_RAISE_ON_WARNINGS` to stabilize connections to older MySQL servers (Issue #31).

### Added
- **Standalone Execution:** Added `__main__.py` to allow running the package directly via `python -m mysql_mcp_server` (Issue #12).

## [0.3.0] - 2026-05-31

### Fixed
- **Asynchronous Reliability:** Refactored all blocking database and SSH operations to use background threads via `anyio.to_thread.run_sync`. This prevents the server from hanging in environments like Windows 11 (Issue #54).
- **Graceful Error Reporting:** Implemented global exception handling in tool calls to return clear, actionable error messages to AI agents and users instead of silent failures (Issue #50).
- **Metadata Formatting:** Improved result set handling for `DESCRIBE`, `SHOW COLUMNS`, and other inspection queries, including explicit `NULL` value rendering (PR #38).
- **SQL Injection Risk:** Added strict regex validation for all database and table identifiers (PR #86).

### Added
- **Multi-Database Mode:** `MYSQL_DATABASE` is now optional. When omitted, the server lists all available databases and supports `USE <database>` or fully qualified table names (PR #86, Issue #68, #81).
- **SSH Tunneling:** Built-in support for secure remote database connections via an SSH jump host using `MYSQL_SSH_ENABLE` (PR #64, contributed by @GeorgeLeex).
- **New Inspection Tools:**
    - `get_schema_info`: Detailed column metadata, types, and comments.
    - `get_table_sample`: Quick data previews to understand table content (PR #64, contributed by @GeorgeLeex).
- **SSE/HTTP Transport:** Support for running as an HTTP server by setting `MCP_TRANSPORT=sse` (PR #86).
- **SSL/TLS Support:** Added `MYSQL_SSL_MODE` for encrypted connections.
- **Environment Management:** Added `.env` support and `.env.example` file (PR #69).

### Security
- Added `ToolAnnotations` to `execute_sql` to flag potentially destructive operations to AI agents (PR #78).
- Dockerfile now runs as a non-root `appuser` and follows best practices for secret management.
- Masked sensitive information (passwords, SSH keys) in server logs.

### Changed
- Refactored server initialization into distinct STDIO and SSE transport handlers.
- Updated minimum `mcp` dependency to `1.2.0` for improved stability and security.

## [0.2.2] - 2025-04-18

### Fixed
- Fixed handling of SQL commands that return result sets, including `SHOW INDEX`, `SHOW CREATE TABLE`, and `DESCRIBE`
- Added improved error handling for result fetching operations
- Added additional debug output to aid in troubleshooting

## [0.2.1] - 2025-02-15

### Added
- Support for MYSQL_PORT configuration through environment variables
- Documentation for PORT configuration in README

### Changed
- Updated tests to use handler functions directly
- Refactored database configuration to runtime

## [0.2.0] - 2025-01-20

### Added
- Initial release with MCP server implementation
- Support for SQL queries through MCP interface
- Ability to list tables and read data
