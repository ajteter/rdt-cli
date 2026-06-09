# rdt-cli MCP 改造报告

## 中文

### 改造目标

本次改造的目标是把 `rdt-cli` 增加为一个可部署在 VPS 上的远程只读 MCP
server，使不同位置的 agent 可以通过 MCP 调用同一个 Reddit 访问层。

目标形态：

```text
Agent
  -> HTTPS /mcp
  -> Cloudflare / reverse proxy
  -> VPS 上的 rdt-mcp
  -> 使用 VPS 上 rdt-cli 保存的 browser-cookie credential
  -> 访问 Reddit
```

MCP 访问密钥和 Reddit cookie 是两套凭据：

- `RDT_MCP_API_KEY`：保护 MCP server，agent 调用时携带。
- `~/.config/rdt-cli/credential.json`：由 VPS 上的 MCP server 使用，用于访问 Reddit。

### 改造方案

采用“保留 CLI，新增 MCP 入口”的方案，而不是把原有 CLI 改写成 MCP。

核心设计：

- 新增 `rdt_cli/mcp_server.py`，作为远程 MCP server 实现。
- 复用现有 `RedditClient`、parser 和结构化 envelope，避免从 MCP 中调用 Click CLI
  或解析 stdout。
- 使用 MCP Python SDK 的 FastMCP + Streamable HTTP transport。
- 默认监听 `127.0.0.1:8000`，通过 Caddy/Nginx/Cloudflare 对外暴露 HTTPS。
- MCP endpoint 默认是 `/mcp`。
- MCP server 必须配置 `RDT_MCP_API_KEY` 才能启动。
- 支持这些认证 header：
  - `Authorization: Bearer <key>`
  - `X-API-Key: <key>`
  - `Api-Key: <key>`
  - `API_KEY: <key>`
- 支持 `RDT_MCP_ALLOWED_ORIGINS` 做 Origin 校验。
- 支持 `RDT_MCP_ALLOWED_HOSTS`，适配 MCP SDK 的 DNS rebinding Host 校验。
- 只暴露读工具，写操作完全不暴露。
- 默认 `count=20`，单次调用上限 `50`。

### 改造结果

已新增远程 MCP server 入口：

```bash
rdt-mcp
```

新增依赖和入口：

- `mcp`
- `uvicorn`
- `rdt-mcp = "rdt_cli.mcp_server:main"`

已暴露的只读 MCP tools：

- `health`
- `whoami`
- `browse_subreddit`
- `browse_home_feed`
- `search_reddit`
- `get_post_details`
- `get_subreddit_info`
- `user_analysis`
- `get_saved`
- `get_upvoted`
- `reddit_explain`

新增部署材料：

- `docs/mcp-systemd.md`：systemd + reverse proxy 部署说明。
- `deploy/rdt-mcp.service.example`：systemd unit 示例。
- `deploy/Caddyfile.rdt-mcp.example`：Caddy 反向代理示例。

新增测试：

- `tests/test_mcp_server.py`
  - API key 校验。
  - Origin 校验。
  - Host allowlist 构造。
  - count 上限。
  - post id / username / subreddit / sort 参数归一化。

### 已知问题

- Reddit browser cookie 可能过期。过期后需要更新 VPS 上的
  `~/.config/rdt-cli/credential.json`，然后重启服务。
- 所有 agent 共用 VPS 上同一个 Reddit 身份。当前没有按 agent 区分 Reddit 账号。
- 不同 MCP client 对远程 HTTP MCP 和自定义 header 的支持不同。客户端必须能配置
  MCP URL 和 API key header。
- Cloudflare 橙云不替代应用层认证。仍然需要 `RDT_MCP_API_KEY`，并建议配置
  `RDT_MCP_ALLOWED_ORIGINS` 和 `RDT_MCP_ALLOWED_HOSTS`。
- MCP SDK 自带 Host 校验。如果反向代理把公网域名作为 upstream `Host` 转发，必须把
  该域名加入 `RDT_MCP_ALLOWED_HOSTS`。
- 当前未实现复杂限流、并发控制和 per-key 权限分级。只保留了单次 `count` 上限。
- 当前不暴露任何写操作，包括 upvote、save、subscribe、comment。

### 待处理事项

- 部署时确定真实域名，并配置：
  - `RDT_MCP_ALLOWED_ORIGINS=https://your-domain`
  - `RDT_MCP_ALLOWED_HOSTS=your-domain`
- 在 VPS 上创建 `/etc/rdt-mcp.env`，配置：
  - `RDT_MCP_API_KEY`
  - 可选 `RDT_MCP_API_KEYS`
- 为 systemd 运行用户准备 `~/.config/rdt-cli/credential.json`。
- 部署 systemd unit，并通过 Caddy/Nginx/Cloudflare 暴露 HTTPS。
- 使用真实 agent 做一次端到端 MCP 调用测试。
- 后续可选：增加简单全局限流和并发限制。
- 后续可选：增加多 key 轮换文档或 per-key 权限模型。
- 后续可选：增加 Docker/Compose 部署方式。
- 后续可选：如果确实需要写操作，再设计显式 opt-in 的写工具开关。

## English

### Goal

The goal of this change is to add a remotely deployable, read-only MCP server
to `rdt-cli`, so agents in different locations can access Reddit through one MCP
endpoint running on a VPS.

Target shape:

```text
Agent
  -> HTTPS /mcp
  -> Cloudflare / reverse proxy
  -> rdt-mcp on the VPS
  -> rdt-cli saved browser-cookie credential
  -> Reddit
```

The MCP access key and Reddit cookies are separate credentials:

- `RDT_MCP_API_KEY`: protects the MCP server and is sent by agent clients.
- `~/.config/rdt-cli/credential.json`: used only by the MCP server on the VPS to
  access Reddit.

### Plan

The implementation keeps the existing CLI and adds a new MCP entry point,
instead of rewriting the Click CLI into MCP.

Core design:

- Add `rdt_cli/mcp_server.py` as the remote MCP server implementation.
- Reuse `RedditClient`, parsers, and the existing structured envelope. MCP tools
  do not invoke Click commands or parse CLI stdout.
- Use the MCP Python SDK with FastMCP and Streamable HTTP transport.
- Bind to `127.0.0.1:8000` by default and expose HTTPS through
  Caddy/Nginx/Cloudflare.
- Use `/mcp` as the default MCP endpoint.
- Require `RDT_MCP_API_KEY` before the MCP server can start.
- Support `Authorization: Bearer`, `X-API-Key`, `Api-Key`, and `API_KEY` headers.
- Support Origin validation through `RDT_MCP_ALLOWED_ORIGINS`.
- Support `RDT_MCP_ALLOWED_HOSTS` for the MCP SDK DNS rebinding Host check.
- Expose read-only tools only. Write operations are not exposed.
- Use `count=20` by default and cap each call at `50`.

### Result

The remote MCP server entry point has been added:

```bash
rdt-mcp
```

Added runtime dependencies and script entry:

- `mcp`
- `uvicorn`
- `rdt-mcp = "rdt_cli.mcp_server:main"`

Exposed read-only MCP tools:

- `health`
- `whoami`
- `browse_subreddit`
- `browse_home_feed`
- `search_reddit`
- `get_post_details`
- `get_subreddit_info`
- `user_analysis`
- `get_saved`
- `get_upvoted`
- `reddit_explain`

Added deployment materials:

- `docs/mcp-systemd.md`
- `deploy/rdt-mcp.service.example`
- `deploy/Caddyfile.rdt-mcp.example`

Added tests:

- `tests/test_mcp_server.py`
  - API key checks.
  - Origin checks.
  - Host allowlist construction.
  - count limit.
  - post id / username / subreddit / sort normalization.
