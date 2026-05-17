"""Async client for the Factory API v1."""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

FACTORY_API_BASE = os.getenv("FACTORY_API_BASE", "https://api.factory.ai/v1")
LEGION_COMPUTER_ID = os.getenv(
    "LEGION_COMPUTER_ID", "fc715237-e805-47f3-a590-0b2561fea3e0"
)


class FactoryClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
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
        # API may return {messages: [...]} or a plain list
        if isinstance(data, list):
            return data
        return data.get("messages", data.get("items", []))

    async def spawn_session(
        self,
        prompt: str,
        computer_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Create a new droid session. Returns the session object."""
        url = f"{self._base}/sessions"
        body: dict[str, Any] = {
            "prompt": prompt,
            "computer_id": computer_id or self._computer_id,
        }
        if tags:
            body["tags"] = tags
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body, headers=self._headers())
            resp.raise_for_status()
            return resp.json()
