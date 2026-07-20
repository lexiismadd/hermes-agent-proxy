"""Hermes Agent Proxy — thin proxy from local VS Code/MCP clients to a remote Hermes Agent.

Modes:
  hermes-agent-proxy acp      — ACP agent (full coding: diffs, terminal, approvals)
  hermes-agent-proxy mcp      — MCP server (tool passthrough)
  hermes-agent-proxy install  — Install user systemd services
  hermes-agent-proxy status   — Check remote health and service status

Configuration via .env file in the working directory or environment variables:
  HERMES_REMOTE_URL   — Remote Hermes API server URL
  HERMES_REMOTE_KEY   — API server key
  HERMES_PROFILE      — Profile name (lexi, lana, zaylie) or empty for default
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="hermes-agent-proxy",
        description="Relay local ACP/MCP clients to a remote Hermes Agent",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── acp ──────────────────────────────────────────────────────────
    acp_p = sub.add_parser("acp", help="Run ACP agent (VS Code integration)")
    acp_p.add_argument("profile", nargs="?", default=None,
                       help="Profile to route to (overrides HERMES_PROFILE in .env)")

    # ── mcp ──────────────────────────────────────────────────────────
    mcp_p = sub.add_parser("mcp", help="Run MCP server (tool passthrough)")
    mcp_p.add_argument("profile", nargs="?", default=None,
                       help="Profile to route to (overrides HERMES_PROFILE in .env)")

    # ── install ──────────────────────────────────────────────────────
    install_p = sub.add_parser("install", help="Install user systemd services")
    install_p.add_argument("--no-acp", action="store_true", help="Skip ACP service")
    install_p.add_argument("--no-mcp", action="store_true", help="Skip MCP service")
    install_p.add_argument("--venv", default=None,
                           help="Python venv path (default: auto-detect)")
    install_p.add_argument("--profile", default=None,
                           help="Profile for the default instance (default: from .env)")

    # ── uninstall ────────────────────────────────────────────────────
    uninstall_p = sub.add_parser("uninstall", help="Remove user systemd services")
    uninstall_p.add_argument("--no-acp", action="store_true")
    uninstall_p.add_argument("--no-mcp", action="store_true")

    # ── status ───────────────────────────────────────────────────────
    sub.add_parser("status", help="Check service status and remote health")

    args = parser.parse_args()

    if args.command == "install":
        _cmd_install(args)
    elif args.command == "uninstall":
        _cmd_uninstall(args)
    elif args.command == "status":
        _cmd_status(args)
    else:
        # acp or mcp — run the relay
        client = _make_client(args.command, args.profile)
        if args.command == "acp":
            _cmd_acp(client)
        else:
            _cmd_mcp(client)


# ── Client factory ────────────────────────────────────────────────────

def _make_client(mode: str, profile_override: str | None = None):
    """Build a RemoteHermesClient from .env + CLI args."""

    # Load .env from working directory
    env_path = HERE / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key in os.environ:
                    continue  # env var already set — don't override
                os.environ[key] = val

    url = os.environ.get("HERMES_REMOTE_URL", "http://172.16.1.231:8642")
    key = os.environ.get("HERMES_REMOTE_KEY", "")
    profile = profile_override or os.environ.get("HERMES_PROFILE", "")

    if not key:
        print("❌ HERMES_REMOTE_KEY not set.", file=sys.stderr)
        print("   Create a .env file: cp .env.example .env", file=sys.stderr)
        print("   Or set: export HERMES_REMOTE_KEY=your-key", file=sys.stderr)
        sys.exit(1)

    from .remote_client import RemoteHermesClient
    return RemoteHermesClient(base_url=url.rstrip("/"), api_key=key, profile=profile)


# ── ACP mode ──────────────────────────────────────────────────────────

def _cmd_acp(client) -> None:
    try:
        import acp
    except ImportError:
        print("❌ 'acp' package required. Install: pip install agent-client-protocol",
              file=sys.stderr)
        sys.exit(1)

    from .acp_relay import HermesRelayACPAgent

    agent = HermesRelayACPAgent(client)
    try:
        asyncio.run(acp.run_agent(agent, use_unstable_protocol=True))
    except KeyboardInterrupt:
        pass


# ── MCP mode ──────────────────────────────────────────────────────────

def _cmd_mcp(client) -> None:
    try:
        from mcp.server import Server  # noqa: F401
    except ImportError:
        print("❌ 'mcp' package required. Install: pip install mcp",
              file=sys.stderr)
        sys.exit(1)

    from .mcp_relay import HermesRelayMCPServer

    server = HermesRelayMCPServer(client)
    asyncio.run(server.run())


# ── Install ───────────────────────────────────────────────────────────

_USER_SYSTEMD = Path.home() / ".config" / "systemd" / "user"

_SVC_TEMPLATES = {
    "acp": """[Unit]
Description=Hermes Relay — ACP agent (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile={workdir}/.env
ExecStart={python} -m hermes_agent_proxy acp %i
WorkingDirectory={workdir}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hermes-agent-proxy-acp

[Install]
WantedBy=default.target
""",
    "mcp": """[Unit]
Description=Hermes Relay — MCP server (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile={workdir}/.env
ExecStart={python} -m hermes_agent_proxy mcp %i
WorkingDirectory={workdir}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hermes-agent-proxy-mcp

[Install]
WantedBy=default.target
""",
}


def _find_python() -> str:
    """Find a Python with hermes_agent_proxy installed."""
    # Check current Python first
    for candidate in [sys.executable, "/usr/bin/python3", "/usr/bin/python"]:
        if Path(candidate).exists():
            result = __import__("subprocess").run(
                [candidate, "-c", "import hermes_agent_proxy"],
                capture_output=True
            )
            if result.returncode == 0:
                return candidate

    # Check venvs
    for venv_base in [Path.home() / ".venv", Path.home() / "venv"]:
        for name in ["bin/python3", "bin/python"]:
            p = venv_base / name
            if p.exists():
                result = __import__("subprocess").run(
                    [str(p), "-c", "import hermes_agent_proxy"],
                    capture_output=True
                )
                if result.returncode == 0:
                    return str(p)

    return sys.executable  # fallback


def _cmd_install(args) -> None:
    subprocess = __import__("subprocess")
    python = args.venv or _find_python()

    # Ensure .env exists
    env_path = HERE / ".env"
    if not env_path.exists():
        example = HERE / ".env.example"
        if example.exists():
            shutil = __import__("shutil")
            shutil.copy(example, env_path)
            print(f"📋 Created .env from .env.example")
            print(f"   Edit {env_path} and set HERMES_REMOTE_KEY")
        else:
            env_path.write_text("HERMES_REMOTE_URL=http://172.16.1.231:8642\nHERMES_REMOTE_KEY=\nHERMES_PROFILE=\n")
            print(f"📋 Created .env — edit {env_path} and set HERMES_REMOTE_KEY")

    # Load env for profile detection
    profile = args.profile
    if not profile and env_path.exists():
        with open(env_path) as f:
            for line in f:
                if line.startswith("HERMES_PROFILE="):
                    profile = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    profile = profile or "default"

    _USER_SYSTEMD.mkdir(parents=True, exist_ok=True)

    install_acp = not args.no_acp
    install_mcp = not args.no_mcp

    if not install_acp and not install_mcp:
        print("Nothing selected (--no-acp --no-mcp)")
        return

    for name in (["acp"] if install_acp else []) + (["mcp"] if install_mcp else []):
        template = _SVC_TEMPLATES[name]
        content = template.format(
            python=python,
            workdir=str(HERE),
        )
        dst = _USER_SYSTEMD / f"hermes-agent-proxy-{name}@.service"
        dst.write_text(content)
        print(f"✅ {dst}")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)

    # Enable instances
    instances = []
    if install_acp:
        instances.append(f"hermes-agent-proxy-acp@{profile}")
    if install_mcp:
        instances.append(f"hermes-agent-proxy-mcp@{profile}")

    for inst in instances:
        r = subprocess.run(["systemctl", "--user", "enable", inst], capture_output=True, text=True)
        if r.returncode == 0:
            print(f"✅ Enabled: {inst}")
        else:
            print(f"⚠️  {inst}: {r.stderr.strip()[:150]}")

    print()
    print("══ Done ══")
    print()
    print("Start:")
    if install_acp:
        print(f"  systemctl --user start hermes-agent-proxy-acp@{profile}")
    if install_mcp:
        print(f"  systemctl --user start hermes-agent-proxy-mcp@{profile}")
    print()
    print("Check:")
    print("  systemctl --user status 'hermes-agent-proxy-*'")


def _cmd_uninstall(args) -> None:
    subprocess = __import__("subprocess")

    for name in (["acp"] if not args.no_acp else []) + (["mcp"] if not args.no_mcp else []):
        svc = _USER_SYSTEMD / f"hermes-agent-proxy-{name}@.service"
        if svc.exists():
            svc.unlink()
            print(f"🗑️  Removed: {svc}")

        # Also disable any running instances
        r = subprocess.run(
            ["systemctl", "--user", "disable", "--now", f"hermes-agent-proxy-{name}@*"],
            capture_output=True, text=True
        )

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print("✅ Uninstalled")


def _cmd_status(args) -> None:
    subprocess = __import__("subprocess")
    import json

    # Check remote health
    try:
        client = _make_client("status")
        healthy = asyncio.run(client.health_check())
        print(f"Remote health: {'✅ OK' if healthy else '❌ unreachable'}")
    except SystemExit:
        print("Remote health: ⚠️  .env not configured")
    except Exception as exc:
        print(f"Remote health: ❌ {exc}")

    # Check systemd services
    for name in ["acp", "mcp"]:
        r = subprocess.run(
            ["systemctl", "--user", "status", f"hermes-agent-proxy-{name}@*"],
            capture_output=True, text=True
        )
        if r.returncode == 0:
            active = [l for l in r.stdout.split("\n") if "active (running)" in l]
            print(f"hermes-agent-proxy-{name}: ✅ {len(active)} running")
        else:
            print(f"hermes-agent-proxy-{name}: ❌ no instances")