"""MCP relay server - transparent tool proxy for remote Hermes MCP servers.

VS Code connects as an MCP client. The relay lists all MCP tools from the
remote Hermes and proxies each tool call through the remote agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .remote_client import RemoteHermesClient

logger = logging.getLogger(__name__)

_PROXY_INSTRUCTION = (
    "You are acting as an MCP tool proxy. The user has invoked the tool below. "
    "Call the tool directly with these exact arguments and return ONLY the raw "
    "tool result as your final response. No explanations, no markdown formatting, "
    "no commentary. Just the tool output."
)


class HermesRelayMCPServer:
    """MCP server that mirrors remote Hermes MCP tools locally."""

    def __init__(self, client: RemoteHermesClient):
        self._client = client

    async def run(self) -> None:
        """Run the MCP server on stdio."""
        from mcp.server import Server
        from mcp.server.stdio import stdio_server

        server = Server("hermes-agent-proxy-mcp")

        @server.list_tools()
        async def list_tools() -> list[Any]:
            from mcp.types import Tool

            tools = await self._discover_tools()
            return [
                Tool(
                    name=t.get("name", ""),
                    description=f"[remote] {t.get('description', '')}",
                    inputSchema=t.get(
                        "inputSchema", t.get("input_schema", t.get("parameters", {}))
                    ),
                )
                for t in tools
                if t.get("name")
            ]

        @server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> list[Any]:
            from mcp.types import TextContent

            prompt = self._build_tool_prompt(name, arguments)
            result_text = ""

            try:
                async for evt in self._client.submit_prompt(
                    text=prompt,
                    session_id=f"mcp-{name}",
                ):
                    etype = evt.get("event", "")
                    if etype == "message.delta":
                        result_text += evt.get("delta", "")
                    elif etype == "run.completed":
                        output = evt.get("output", "")
                        if output:
                            result_text = output
                        break
                    elif etype == "error":
                        result_text = f"Error: {evt.get('message', 'unknown')}"
                        break
            except Exception as exc:
                result_text = f"Proxy error: {exc}"

            return [TextContent(type="text", text=result_text.strip() or "(no result)")]

        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    async def _discover_tools(self) -> list[dict[str, Any]]:
        """Discover available MCP tools from the remote Hermes."""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(
                    self._client._url("/v1/toolsets"),
                    headers={"Authorization": f"Bearer {self._client.api_key}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        caps = await resp.json()
                        tools = caps.get(
                            "tools", caps.get("features", {}).get("tools", [])
                        )
                        if tools:
                            return tools
            except Exception:
                pass

        return []

    @staticmethod
    def _build_tool_prompt(name: str, arguments: dict[str, Any]) -> str:
        args_json = json.dumps(arguments, indent=2)
        return (
            f"{_PROXY_INSTRUCTION}\n\n"
            f"Tool: {name}\n"
            f"Arguments:\n```json\n{args_json}\n```"
        )
