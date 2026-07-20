"""ACP relay agent - bridges ACP protocol to remote Hermes API Server.

VS Code talks ACP to this relay. The relay translates to POST /v1/runs
on the remote Hermes and streams SSE events back as ACP session updates.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import acp
from acp.schema import (
    AgentCapabilities,
    AuthenticateResponse,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    SessionCapabilities,
    SessionForkCapabilities,
    SessionListCapabilities,
    SessionResumeCapabilities,
    SessionInfo,
    SessionMode,
    SessionModeState,
    SetSessionModeResponse,
    SetSessionConfigOptionResponse,
    ForkSessionResponse,
    ResumeSessionResponse,
    TextContentBlock,
)

from .remote_client import RemoteHermesClient

logger = logging.getLogger(__name__)


class HermesRelayACPAgent(acp.Agent):
    """ACP agent that relays everything to the remote Hermes Agent."""

    def __init__(self, client: RemoteHermesClient):
        super().__init__()
        self._client = client
        self._conn: acp.Client | None = None
        # session_id -> {cwd, history, current_run_id}
        self._sessions: dict[str, dict[str, Any]] = {}

    @property
    def _profile_label(self) -> str:
        return self._client.profile or "default"

    def on_connect(self, conn: acp.Client) -> None:
        self._conn = conn
        logger.info("ACP connected (relay → %s @ %s)", self._profile_label, self._client.base_url)

    # ── Protocol handshake ──────────────────────────────────────────────

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        healthy = await self._client.health_check()
        if not healthy:
            logger.warning("Remote Hermes health check failed")

        return InitializeResponse(
            protocol_version=0,
            agent_capabilities=AgentCapabilities(
                prompt=PromptCapabilities(
                    text=True,
                    image=False,
                    audio=False,
                ),
                session=SessionCapabilities(
                    fork=SessionForkCapabilities(),
                    list=SessionListCapabilities(),
                    resume=SessionResumeCapabilities(),
                ),
            ),
            agent_info=Implementation(
                name="Hermes Relay",
                title=f"Hermes Relay → {self._profile_label} @ {self._client.base_url}",
                version="0.1.0",
            ),
        )

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse | None:
        return None

    # ── Session management ──────────────────────────────────────────────

    async def new_session(
        self, cwd: str, additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None, **kwargs: Any,
    ) -> NewSessionResponse:
        sid = f"relay_{uuid.uuid4().hex[:12]}"
        self._sessions[sid] = {"cwd": cwd, "history": [], "run_id": None}
        logger.info("New session: %s", sid)
        return NewSessionResponse(session_id=sid)

    async def load_session(
        self, cwd: str, session_id: str, mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None, **kwargs: Any,
    ) -> LoadSessionResponse | None:
        if session_id not in self._sessions:
            return None
        return LoadSessionResponse(session_id=session_id)

    async def resume_session(
        self, session_id: str, cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None, **kwargs: Any,
    ) -> ResumeSessionResponse:
        ok = session_id in self._sessions
        return ResumeSessionResponse(session_id=session_id, success=ok)

    async def fork_session(
        self, session_id: str, cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None, **kwargs: Any,
    ) -> ForkSessionResponse:
        new_id = f"relay_{uuid.uuid4().hex[:12]}"
        old = self._sessions.get(session_id, {})
        self._sessions[new_id] = {"cwd": old.get("cwd", cwd), "history": list(old.get("history", []))}
        return ForkSessionResponse(session_id=new_id)

    async def list_sessions(self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any) -> ListSessionsResponse:
        sessions = [SessionInfo(id=sid, cwd=st.get("cwd", "/")) for sid, st in self._sessions.items()]
        return ListSessionsResponse(sessions=sessions, next_cursor=None)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        state = self._sessions.get(session_id)
        if state and state.get("run_id"):
            await self._client.stop_run(state["run_id"])

    async def set_session_mode(self, session_id: str, mode_id: str, **kwargs: Any) -> SetSessionModeResponse | None:
        return SetSessionModeResponse()

    async def set_config_option(self, config_id: str, session_id: str, value: str | bool, **kwargs: Any) -> SetSessionConfigOptionResponse | None:
        return SetSessionConfigOptionResponse()

    # ── The main prompt handler ─────────────────────────────────────────

    async def prompt(
        self,
        prompt: list[TextContentBlock],
        session_id: str,
        **kwargs: Any,
    ) -> PromptResponse:
        conn = self._conn
        if conn is None:
            return PromptResponse(stop_reason="refusal")

        user_text = "".join(
            b.text for b in prompt if isinstance(b, TextContentBlock)
        ).strip()
        if not user_text:
            return PromptResponse(stop_reason="end_turn")

        logger.info("Relay [%s]: %s", session_id, user_text[:80])

        state = self._sessions.get(session_id)
        if state is None:
            state = {"cwd": "/", "history": [], "run_id": None}
            self._sessions[session_id] = state

        # Track tool calls for ACP rendering
        active_tools: dict[str, str] = {}  # call_id -> tool_name

        async def _send(update: Any) -> None:
            if conn:
                try:
                    await conn.session_update(session_id, update)
                except Exception:
                    pass

        try:
            async for evt in self._client.submit_prompt(
                text=user_text,
                session_id=session_id,
                conversation_history=state.get("history"),
            ):
                etype = evt.get("event", "")

                if etype == "run.started":
                    state["run_id"] = evt.get("run_id")
                    # Announce the session via session_update so the client
                    # sees the agent is working
                    await _send(SessionNotification(session_id=session_id))

                elif etype == "message.delta":
                    delta = evt.get("delta", "")
                    if delta:
                        await _send(acp.update_agent_message_text(delta))

                elif etype == "tool.started":
                    name = evt.get("tool", evt.get("tool_name", "unknown"))
                    cid = f"call_{uuid.uuid4().hex[:8]}"
                    active_tools[cid] = name
                    await _send(acp.start_tool_call(cid, name, {}))

                elif etype == "tool.completed":
                    name = evt.get("tool", evt.get("tool_name", ""))
                    err = evt.get("error", False)
                    status = "failed" if err else "completed"
                    matching = None
                    for cid, tn in list(active_tools.items()):
                        if tn == name:
                            matching = cid
                            break
                    if matching:
                        await _send(acp.update_tool_call(matching, status))
                        del active_tools[matching]

                elif etype == "reasoning.available":
                    text = evt.get("text", "")
                    if text:
                        await _send(acp.update_agent_thought_text(text))

                elif etype == "assistant.completed":
                    content = evt.get("content", "")
                    state["history"].append({"role": "user", "content": user_text})
                    if content:
                        state["history"].append({"role": "assistant", "content": content})

                elif etype == "run.completed":
                    output = evt.get("output", "")
                    usage = evt.get("usage", {})
                    logger.info("Run completed: tokens=%s", usage)
                    if output and not state["history"] or (
                        state["history"] and state["history"][-1].get("role") != "assistant"
                    ):
                        state["history"].append({"role": "assistant", "content": output})

                elif etype == "error":
                    msg = evt.get("message", "Unknown error")
                    await _send(acp.update_agent_message_text(f"\n❌ {msg}\n"))

        except Exception as exc:
            logger.exception("Relay prompt failed")
            await _send(acp.update_agent_message_text(f"\n❌ Relay error: {exc}\n"))

        # Clean up orphaned tool calls
        for cid in active_tools:
            try:
                await _send(acp.update_tool_call(cid, "failed"))
            except Exception:
                pass

        return PromptResponse(stop_reason="end_turn")