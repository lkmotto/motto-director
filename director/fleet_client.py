"""Thin async client for the ONA Fleet MCP server (JSON-RPC over HTTP)."""
from __future__ import annotations

import itertools
import os
from typing import Any, Optional

import httpx

_ID = itertools.count(1)

MCP_URL = os.getenv(
    "MOTTO_MCP_URL",
    "https://p01--motto-mcp-server--hq2dk45g4bfc.code.run/mcp",
)


class FleetClient:
    def __init__(
        self,
        url: str = MCP_URL,
        auth_token: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self._url = url
        self._token = auth_token or os.environ["MOTTO_MCP_AUTH_TOKEN"]
        self._timeout = timeout

    async def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
            "id": next(_ID),
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result")

    async def record_run_start(
        self,
        agent_name: str,
        kind: str,
        intent: Optional[str] = None,
        parent_run_id: Optional[str] = None,
    ) -> str:
        """Returns run_id."""
        result = await self._call(
            "record_run_start",
            {
                "agent_name": agent_name,
                "kind": kind,
                "intent": intent,
                "parent_run_id": parent_run_id,
            },
        )
        return result["run_id"]

    async def record_run_end(
        self,
        run_id: str,
        status: str,
        summary: Optional[dict] = None,
    ) -> None:
        await self._call(
            "record_run_end",
            {"run_id": run_id, "status": status, "summary": summary},
        )

    async def record_artifact_content(
        self,
        agent_name: str,
        kind: str,
        body: str,
        intent: Optional[str] = None,
        run_id: Optional[str] = None,
        name: Optional[str] = None,
        meta: Optional[dict] = None,
        send_blocking: bool = False,
    ) -> int:
        """Returns artifact_id."""
        result = await self._call(
            "record_artifact_content",
            {
                "agent_name": agent_name,
                "kind": kind,
                "body": body,
                "intent": intent,
                "run_id": run_id,
                "name": name,
                "meta": meta,
                "send_blocking": send_blocking,
            },
        )
        # result is typically the stored artifact row; grab id if present
        if isinstance(result, dict):
            return result.get("id") or result.get("artifact_id")
        return result

    async def record_event(
        self,
        agent_name: str,
        kind: str,
        level: str = "info",
        payload: Optional[dict] = None,
        run_id: Optional[str] = None,
    ) -> None:
        await self._call(
            "record_event",
            {
                "agent_name": agent_name,
                "kind": kind,
                "level": level,
                "payload": payload,
                "run_id": run_id,
            },
        )
