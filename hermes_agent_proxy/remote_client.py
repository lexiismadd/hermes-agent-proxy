"""HTTP client for the remote Hermes Agent API Server."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger(__name__)


class RemoteHermesClient:
    """Async HTTP client for the remote Hermes Agent API Server.

    Talks to the remote over the Runs API (POST /v1/runs + SSE events)
    and the profile-prefixed endpoints when a profile is configured.
    """

    def __init__(self, base_url: str, api_key: str, profile: str = ""):
        self.base_url = base_url
        self.api_key = api_key
        self.profile = profile

    @property
    def _prefix(self) -> str:
        if self.profile:
            return f"/p/{self.profile}"
        return ""

    def _url(self, path: str) -> str:
        return f"{self.base_url}{self._prefix}{path}"

    async def health_check(self) -> bool:
        """Verify the remote is reachable."""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(
                    self._url("/health"),
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    return resp.status == 200
            except Exception as exc:
                logger.warning("Health check failed: %s", exc)
                return False

    async def submit_prompt(
        self,
        text: str,
        *,
        session_id: str | None = None,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Submit a prompt to the remote and stream SSE events.

        Uses POST /v1/runs + GET /v1/runs/{id}/events SSE stream.

        The remote Hermes uses this SSE format:
            data: {"event": "message.delta", "delta": "Hello"}

        i.e. the event type is embedded in the JSON payload on the data: line.

        Yields event dicts: {"event": "message.delta", "delta": "Hello", ...}
        """
        import aiohttp

        run_id = f"run_{uuid.uuid4().hex}"
        session_id = session_id or run_id

        payload: dict[str, Any] = {
            "input": text,
            "session_id": session_id,
        }
        if conversation_history:
            payload["conversation_history"] = conversation_history

        async with aiohttp.ClientSession() as session:
            # POST /v1/runs to start the run
            async with session.post(
                self._url("/v1/runs"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 202:
                    body = await resp.text()
                    yield {
                        "event": "error",
                        "message": f"Run submission failed ({resp.status}): {body}",
                    }
                    return
                run_info = await resp.json()
                returned_run_id = run_info.get("run_id", run_id)

            # GET SSE stream of events
            # Format: data: {"event": "message.delta", ...}\n\n
            try:
                async with session.get(
                    self._url(f"/v1/runs/{returned_run_id}/events"),
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=aiohttp.ClientTimeout(total=600, sock_read=120),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        yield {
                            "event": "error",
                            "message": f"Events stream failed ({resp.status}): {body}",
                        }
                        return

                    # Parse SSE: chunk-based, split on \n\n
                    remainder = ""
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                resp.content.read(65536), timeout=120
                            )
                        except asyncio.TimeoutError:
                            yield {"event": "error", "message": "Stream timeout"}
                            return
                        if not chunk:
                            break

                        text = remainder + chunk.decode("utf-8")
                        remainder = ""

                        parts = text.split("\n\n")
                        # Last part may be incomplete - save for next chunk
                        if not text.endswith("\n\n"):
                            remainder = parts.pop()

                        for part in parts:
                            part = part.strip()
                            if not part or part.startswith(":"):
                                continue

                            # Each SSE event is: data: {"event": "...", ...}
                            if part.startswith("data: "):
                                data_str = part[6:]
                                try:
                                    parsed = json.loads(data_str)
                                    yield parsed  # already contains "event" key
                                except json.JSONDecodeError:
                                    continue

            except aiohttp.ClientError as exc:
                yield {"event": "error", "message": f"Stream connection error: {exc}"}
            except asyncio.TimeoutError:
                yield {"event": "error", "message": "Stream timeout"}

    async def submit_approval(
        self,
        run_id: str,
        request_id: str,
        choice: str,
    ) -> dict[str, Any] | None:
        """Resolve a pending approval."""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._url(f"/v1/runs/{run_id}/approval"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"request_id": request_id, "choice": choice},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None

    async def stop_run(self, run_id: str) -> bool:
        """Interrupt a running agent turn."""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._url(f"/v1/runs/{run_id}/stop"),
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return resp.status == 200
