"""Async client for the Factory API v0."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

FACTORY_API_BASE = os.getenv("FACTORY_API_BASE", "https://api.factory.ai/api/v0")
LEGION_COMPUTER_ID = os.getenv(
    "FACTORY_COMPUTER_ID",
    os.getenv("LEGION_COMPUTER_ID", "fc715237-e805-47f3-a590-0b2561fea3e0"),
)

SUCCESS_STATUSES = {"idle", "completed", "complete", "done", "finished", "success", "succeeded"}
FAILURE_STATUSES = {
    "failed",
    "error",
    "errored",
    "cancelled",
    "canceled",
    "timeout",
    "timed_out",
    "aborted",
}
TERMINAL_STATUSES = SUCCESS_STATUSES | FAILURE_STATUSES


class FactoryClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = FACTORY_API_BASE,
        computer_id: str = LEGION_COMPUTER_ID,
        timeout: float = 30.0,
    ):
        self._key = api_key or os.environ["FACTORY_API_KEY"]
        self._base = base_url.rstrip("/")
        self._computer_id = computer_id
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    async def get_session(self, session_id: str) -> dict[str, Any]:
        """Fetch session metadata / status."""
        url = f"{self._base}/sessions/{session_id}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Return all messages for a session (oldest first)."""
        url = f"{self._base}/sessions/{session_id}/messages"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("messages", data.get("items", []))

    async def spawn_session(
        self,
        prompt: str,
        computer_id: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new droid session and send the initial prompt. Returns the session object."""
        sessions_url = f"{self._base}/sessions"
        body: dict[str, Any] = {
            "computerId": computer_id or self._computer_id,
        }
        if tags:
            body["sessionSettings"] = {"tags": [{"name": t} for t in tags]}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(sessions_url, json=body, headers=self._headers())
            resp.raise_for_status()
            session = resp.json()

        session_id = self._extract_session_id(session)
        if session_id and prompt:
            msg_url = f"{self._base}/sessions/{session_id}/messages"
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                msg_resp = await client.post(
                    msg_url,
                    json={"text": prompt},
                    headers=self._headers(),
                )
                msg_resp.raise_for_status()

        return session

    async def spawn_swarm(self, prompts: list[str]) -> list[str]:
        if not prompts:
            return []
        results = await asyncio.gather(
            *(self.spawn_session(prompt=p) for p in prompts),
            return_exceptions=True,
        )
        session_ids: list[str] = []
        for item in results:
            if isinstance(item, Exception):
                session_ids.append("")
                continue
            session_ids.append(self._extract_session_id(item))
        return session_ids

    async def get_session_status(self, session_id: str) -> str:
        session = await self.get_session(session_id)
        status = self._extract_status(session)
        return (status or "").strip().lower()

    async def is_idle(self, session_id: str) -> bool:
        status = await self.get_session_status(session_id)
        return status in TERMINAL_STATUSES

    async def get_final_output(self, session_id: str) -> str:
        messages = await self.get_messages(session_id)
        for message in reversed(messages):
            role = str(message.get("role", "")).lower()
            if role not in {"assistant", "droid"}:
                continue
            text = self._extract_message_text(message.get("content"))
            if text:
                return text
        session = await self.get_session(session_id)
        for key in ("final_output", "output", "summary"):
            value = session.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _extract_session_id(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        # v0 API returns sessionId
        for key in ("sessionId", "id", "session_id"):
            sid = payload.get(key)
            if isinstance(sid, str) and sid:
                return sid
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("sessionId", "id", "session_id"):
                sid = data.get(key)
                if isinstance(sid, str) and sid:
                    return sid
        return ""

    def _extract_status(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        status = payload.get("status")
        if isinstance(status, str):
            return status
        data = payload.get("data")
        if isinstance(data, dict):
            status = data.get("status")
            if isinstance(status, str):
                return status
        state = payload.get("state")
        if isinstance(state, str):
            return state
        return ""

    def _extract_message_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                    continue
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(p for p in parts if p).strip()
        if isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str):
                return text.strip()
        return ""
