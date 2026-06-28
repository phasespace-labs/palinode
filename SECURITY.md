# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Palinode, please report it responsibly.

**Email:** paul@phasespace.co

**What to include:**
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if you have one)

**Response timeline:**
- Acknowledgment within 48 hours
- Assessment and plan within 7 days
- Fix released as soon as practical, with credit to the reporter (unless you prefer anonymity)

**Please do not:**
- Open a public GitHub issue for security vulnerabilities
- Exploit the vulnerability beyond what's needed to demonstrate it

## Scope

Palinode runs locally on your machine. The primary attack surface is:
- Path traversal in file operations (mitigated: all paths validated against PALINODE_DIR)
- API endpoint abuse (mitigated: rate limiting, request size limits, optional bearer auth — see below)
- LLM prompt injection via memory content (mitigated: deterministic executor, LLM never writes files directly)

## API authentication

The Palinode API server (default port 6340) supports an optional bearer-token
auth layer. It is **off by default** to keep local-first development friction
free and **required** when binding the API to a non-loopback address.

| Deployment | Recommended setting | Notes |
|------------|---------------------|-------|
| Local dev (single user, loopback) | No token | Default. The middleware is a no-op when `PALINODE_API_TOKEN` is unset. |
| Multi-user / homelab / Tailscale | Set `PALINODE_API_TOKEN` | Every request must carry `Authorization: Bearer <token>` except `/health` and `/health/watcher`. |
| Public exposure (`PALINODE_API_BIND_INTENT=public`) | **Token required** | The server refuses to start without `PALINODE_API_TOKEN` (or `PALINODE_API_TOKEN_FILE`). |

### Generating a token

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Set it in the API server's environment:

```bash
export PALINODE_API_TOKEN=<value>
# or, for docker-secrets / sealed-secrets style deployments:
export PALINODE_API_TOKEN_FILE=/run/secrets/palinode_api_token
```

`PALINODE_API_TOKEN` takes precedence over `PALINODE_API_TOKEN_FILE` when both
are set. Whitespace is stripped. An empty value is treated as "no token".

### Using the token from a client

```bash
curl -H "Authorization: Bearer $PALINODE_API_TOKEN" \
     http://localhost:6340/list
```

For MCP clients (Claude Code, Zed, Cursor, etc.) over Streamable HTTP, see
[`docs/INSTALL-CLAUDE-CODE.md`](docs/INSTALL-CLAUDE-CODE.md) for the
`headers` block to add to your MCP config.

### Rotating

There is no on-disk token store. To rotate, change the env var (or the file)
and restart the API server. Existing connections fail closed with `401
Unauthorized` and clients reconnect with the new token.

### What this does NOT cover

- The MCP HTTP server (`palinode-mcp-http` / `palinode-mcp-sse` on port 6341)
  currently has no auth layer — that is tracked separately and has different
  transport semantics (SSE/Streamable HTTP) than the REST API. If you need
  to expose the MCP endpoint beyond loopback today, front it with a reverse
  proxy that enforces auth, or restrict access at the network layer (VPN,
  Tailscale ACLs, firewall).

The token comparison is constant-time (`hmac.compare_digest`) and the
expected header is pre-encoded at startup, so the hot path is a single
constant-time byte compare with no per-request format work.

## Supported Versions

Security fixes are applied to the latest release only.
