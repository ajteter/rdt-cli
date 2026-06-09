# Remote MCP Deployment

This guide runs the read-only rdt-cli MCP server on a VPS with systemd and
exposes it through a reverse proxy such as Caddy or Nginx behind Cloudflare.

## Server Shape

```text
Agent clients
  -> https://rdtmcp.example.com/mcp
  -> Cloudflare
  -> reverse proxy on the VPS
  -> 127.0.0.1:8000
  -> rdt-mcp
  -> reddit.com with rdt-cli browser cookies
```

The MCP API key protects the MCP server. It is separate from Reddit cookies.

## Environment

Create `/etc/rdt-mcp.env`:

```dotenv
RDT_MCP_API_KEY=replace-with-a-random-long-secret

RDT_MCP_HOST=127.0.0.1
RDT_MCP_PORT=8000
RDT_MCP_PATH=/mcp

# Set this after the domain is known. Requests without Origin are still allowed.
RDT_MCP_ALLOWED_ORIGINS=https://rdtmcp.example.com

# Host header allowlist for MCP SDK DNS rebinding protection.
# This is also derived from RDT_MCP_ALLOWED_ORIGINS, but keeping it explicit
# makes reverse proxy behavior easier to audit.
RDT_MCP_ALLOWED_HOSTS=rdtmcp.example.com
```

Optional settings:

```dotenv
# Comma-separated extra keys. Useful when rotating keys or separating agents.
RDT_MCP_API_KEYS=another-secret,third-secret

# Only use this for local debugging.
RDT_MCP_ALLOW_ANY_ORIGIN=false

# Uvicorn log level.
RDT_MCP_LOG_LEVEL=info
```

Protect the env file:

```bash
sudo chown root:root /etc/rdt-mcp.env
sudo chmod 600 /etc/rdt-mcp.env
```

## Reddit Credential

`rdt-mcp` uses the same saved browser-cookie credential as `rdt`:

```text
~/.config/rdt-cli/credential.json
```

On the server, run the login flow once as the same user that will run the
systemd service:

```bash
rdt login
rdt status --json
```

For non-interactive servers, copy a credential file exported from a trusted
machine and protect it:

```bash
sudo mkdir -p /opt/rdt-cli/.config/rdt-cli
sudo install -o rdt-mcp -g rdt-mcp -m 600 credential.json \
  /opt/rdt-cli/.config/rdt-cli/credential.json
```

Cookies can expire. Refresh the credential and restart the service when
`health` reports an authentication error.

## Install

Example layout:

```bash
sudo useradd --system --home /opt/rdt-cli --shell /usr/sbin/nologin rdt-mcp
sudo mkdir -p /opt/rdt-cli
sudo chown rdt-mcp:rdt-mcp /opt/rdt-cli

sudo -u rdt-mcp git clone https://github.com/jackwener/rdt-cli.git /opt/rdt-cli/src
cd /opt/rdt-cli/src
sudo -u rdt-mcp uv sync
```

Install the systemd unit:

```bash
sudo cp deploy/rdt-mcp.service.example /etc/systemd/system/rdt-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now rdt-mcp
sudo journalctl -u rdt-mcp -f
```

## Reverse Proxy

Caddy example:

```caddyfile
rdtmcp.example.com {
	reverse_proxy 127.0.0.1:8000
}
```

Nginx example:

```nginx
server {
    listen 443 ssl;
    server_name rdtmcp.example.com;

    location /mcp {
        proxy_pass http://127.0.0.1:8000/mcp;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 1h;
    }
}
```

If Cloudflare orange cloud is enabled, keep the service bound to
`127.0.0.1:8000`. For stronger origin protection, also restrict direct inbound
traffic to the VPS or use Cloudflare Tunnel.

If your reverse proxy rewrites the upstream `Host` header to `127.0.0.1:8000`,
the default local Host allowlist is enough. If it forwards the public domain as
the Host header, set `RDT_MCP_ALLOWED_HOSTS` to that domain.

## Client Configuration

Use the Streamable HTTP endpoint:

```text
https://rdtmcp.example.com/mcp
```

Send one of these headers:

```http
Authorization: Bearer replace-with-a-random-long-secret
```

or:

```http
X-API-Key: replace-with-a-random-long-secret
```

The server also accepts `Api-Key` and `API_KEY` headers for clients with limited
header naming options.

## Exposed Tools

The server is read-only. It does not expose upvote, downvote, save, unsave,
subscribe, unsubscribe, comment, or other write operations.

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

Default `count` is 20. A single tool call is capped at 50 results.

## Updating Cookies

If `health` or `whoami` reports an authentication error, update the saved
credential and restart:

```bash
sudo systemctl restart rdt-mcp
```
