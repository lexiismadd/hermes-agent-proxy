# Hermes Agent Proxy

Thin proxy that lets VS Code and MCP clients on your local machine talk to a remote Hermes Agent server. Two modes:

- **`hermes-agent-proxy acp`** — full agent inside VS Code (diffs, terminal, approvals, tool calls)
- **`hermes-agent-proxy mcp`** — transparent MCP tool proxy (ha-mcp, n8n, unclick, etc.)

No model, no tools, no skills on your laptop - just a ~50 MB wire-format translator.

## Architecture

```
┌───────────┐   ACP/MCP       ┌──────────────────┐    HTTP          ┌──────────────────┐
│  VS Code  │ ◄─────────────► │ Hermes-Agent-    │ ◄────────────── │  Remote Hermes   │
│  (laptop) │   local stdio   │ Proxy (~50 MB)   │  LAN or tunnel  │  (your server)   │
└───────────┘                 │                  │                 │                  │
                              │ no model / tools │                 │  API Server      │
                              │ no skills/memory │                 │  0.0.0.0:8642    │
                              └──────────────────┘                 │                  │
                                                                   │  All skills      │
                                                                   │  All MCP servers │
                                                                   │  All memory      │
                                                                   │  All API keys    │
                                                                   └──────────────────┘
```

## Quick start

```bash
# Clone
git clone <your-repo-url> hermes-agent-proxy
cd hermes-agent-proxy

# Create venv and install
python3 -m venv ~/.venv
~/.venv/bin/pip install -e ".[acp,mcp]"

# Configure
cp .env.example .env
# Edit .env — set HERMES_REMOTE_KEY

# Install as user systemd services (auto-start on login)
~/.venv/bin/python3 -m hermes_agent_proxy install
systemctl --user start hermes-agent-proxy-acp@lexi
systemctl --user start hermes-agent-proxy-mcp@lexi

# Or run directly (foreground)
HERMES_REMOTE_KEY=your-key ~/.venv/bin/python3 -m hermes_agent_proxy acp lexi
```

## VS Code setup

### ACP mode (full agent)

1. Install the [ACP Client](https://marketplace.visualstudio.com/items?itemName=formulahendry.acp-client) extension
2. Configure the agent in VS Code settings (`acp.agents`):
```json
{
  "acp.agents": {
    "Hermes Proxy": {
      "command": "/home/you/.venv/bin/python3",
      "args": ["-m", "hermes_agent_proxy", "acp", "lexi"]
    }
  }
}
```

### MCP mode (tool passthrough)

Configure in VS Code's `.vscode/mcp.json`:
```json
{
  "servers": {
    "hermes-agent-proxy": {
      "command": "/home/you/.venv/bin/python3",
      "args": ["-m", "hermes_agent_proxy", "mcp", "lexi"]
    }
  }
}
```

## CLI reference

```
hermes-agent-proxy acp [profile]     — Run ACP agent
hermes-agent-proxy mcp [profile]     — Run MCP server
hermes-agent-proxy install           — Install user systemd services
hermes-agent-proxy uninstall         — Remove user systemd services
hermes-agent-proxy status            — Check remote health + service status
```

## Systemd services

```bash
# Install
python3 -m hermes_agent_proxy install              # Both ACP + MCP
python3 -m hermes_agent_proxy install --no-mcp     # ACP only
python3 -m hermes_agent_proxy install --no-acp     # MCP only

# Per-profile instances
systemctl --user enable --now hermes-agent-proxy-acp@lexi
systemctl --user enable --now hermes-agent-proxy-acp@lana
systemctl --user enable --now hermes-agent-proxy-mcp@lexi
```

The `@profile` suffix becomes `%i` in the service — each profile gets its own isolated service instance reading the same `.env`.

## Configuration (.env)

```bash
# Remote Hermes API Server URL
HERMES_REMOTE_URL=http://172.16.1.231:8642

# API server key
HERMES_REMOTE_KEY=your-key-here

# Default profile (lexi, lana, zaylie) or empty for default
HERMES_PROFILE=lexi
```

Environment variables can also be set directly (override `.env`).

## Requirements

- Python 3.11+
- `aiohttp` (always)
- `agent-client-protocol` (for ACP mode)
- `mcp` (for MCP mode)
- Remote Hermes with `API_SERVER_ENABLED=true` and network-reachable

## License

MIT
