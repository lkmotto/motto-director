import itertools
import json
import os
from typing import Any, Optional

import httpx

_ID = itertools.count(1)

ONA_URL = os.getenv(
    'ONA_FLEET_URL',
    os.getenv('MOTTO_MCP_URL', 'https://ona-mcp-proxy.ljm32901.workers.dev/mcp'),
)
ONA_TOKEN = os.getenv('MOTTO_MCP_AUTH_TOKEN', '')


def _parse_sse(text: str) -> Any:
    """Extract the first data payload from an SSE response."""
    for line in text.splitlines():
        if line.startswith('data:'):
            raw = line[5:].strip()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
    # Fallback: try the whole body
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {'raw': text}


def _unwrap_result(result: Any) -> Any:
    """Unwrap MCP content array to parsed value when present."""
    if not isinstance(result, dict):
        return result
    content = result.get('content')
    if not content or not isinstance(content, list):
        return result
    first = content[0] if content else {}
    text = first.get('text', '') if isinstance(first, dict) else ''
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {'raw': text}


class FleetClient:
    def __init__(self, url: str = None, auth_token: str = None, timeout: float = 30.0):
        self._url = url or ONA_URL
        self._token = auth_token or ONA_TOKEN
        self._timeout = timeout
        self._session_id: Optional[str] = None

    def _base_headers(self) -> dict:
        h = {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
        }
        if self._token:
            h['Authorization'] = f'Bearer {self._token}'
        return h

    async def _ensure_session(self) -> None:
        if self._session_id:
            return
        payload = {
            'jsonrpc': '2.0',
            'method': 'initialize',
            'params': {
                'protocolVersion': '2024-11-05',
                'capabilities': {},
                'clientInfo': {'name': 'motto-director', 'version': '1.0'},
            },
            'id': 0,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._url, json=payload, headers=self._base_headers())
            resp.raise_for_status()
            self._session_id = resp.headers.get('mcp-session-id') or resp.headers.get('Mcp-Session-Id')
        if not self._session_id:
            raise RuntimeError('MCP session initialization failed: no Mcp-Session-Id returned')

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        await self._ensure_session()
        bare_name = tool_name.split('___', 1)[-1] if '___' in tool_name else tool_name
        payload = {
            'jsonrpc': '2.0',
            'method': 'tools/call',
            'params': {'name': bare_name, 'arguments': arguments},
            'id': next(_ID),
        }
        headers = dict(self._base_headers())
        headers['Mcp-Session-Id'] = self._session_id

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._url, json=payload, headers=headers)
            if resp.status_code == 400 and 'Missing session ID' in resp.text:
                # Session expired - reinitialize
                self._session_id = None
                await self._ensure_session()
                headers['Mcp-Session-Id'] = self._session_id
                resp = await client.post(self._url, json=payload, headers=headers)
            resp.raise_for_status()
            data = _parse_sse(resp.text)

        if isinstance(data, dict) and 'error' in data:
            raise RuntimeError(f'MCP error: {data["error"]}')

        result = data.get('result') if isinstance(data, dict) else data
        return _unwrap_result(result)

    async def get_fleet_status(self) -> list[dict]:
        result = await self.call_tool('ona-mcp___get_fleet_status', {})
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get('agents', result.get('data', []))
        return []

    async def record_run_start(self, agent_name: str, kind: str, intent: str = None,
                                parent_run_id: str = None) -> str:
        args: dict = {'agent_name': agent_name, 'kind': kind}
        if intent is not None:
            args['intent'] = intent
        if parent_run_id is not None:
            args['parent_run_id'] = parent_run_id
        result = await self.call_tool('ona-mcp___record_run_start', args)
        if isinstance(result, dict):
            return result.get('run_id', result.get('id', ''))
        return str(result) if result else ''

    async def record_run_end(self, run_id: str, status: str, summary: dict = None):
        args: dict = {'run_id': run_id, 'status': status}
        if summary is not None:
            args['summary'] = summary
        await self.call_tool('ona-mcp___record_run_end', args)

    async def record_event(self, agent_name: str, kind: str, level: str = 'info',
                            payload: dict = None, run_id: str = None):
        args: dict = {'agent_name': agent_name, 'kind': kind, 'level': level}
        if payload is not None:
            args['payload'] = payload
        if run_id is not None:
            args['run_id'] = run_id
        await self.call_tool('ona-mcp___record_event', args)

    async def record_artifact(self, agent_name: str, kind: str, body: str,
                               intent: str = None, run_id: str = None,
                               name: str = None, meta: dict = None):
        args: dict = {'agent_name': agent_name, 'kind': kind, 'body': body}
        if intent is not None:
            args['intent'] = intent
        if run_id is not None:
            args['run_id'] = run_id
        if name is not None:
            args['name'] = name
        if meta is not None:
            args['meta'] = meta
        result = await self.call_tool('ona-mcp___record_artifact_content', args)
        if isinstance(result, dict):
            return result.get('id') or result.get('artifact_id')
        return result

    record_artifact_content = record_artifact

    async def heartbeat(self, agent_name: str, status: dict = None):
        args: dict = {'agent_name': agent_name}
        if status is not None:
            args['status'] = status
        await self.call_tool('ona-mcp___heartbeat', args)

    async def consume_open_intents(self, agent_name: str, limit: int = 10) -> list[dict]:
        result = await self.call_tool(
            'ona-mcp___consume_open_intents',
            {'agent_name': agent_name, 'limit': limit},
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get('intents', result.get('data', []))
        return []

    async def signal_intent(
        self,
        target_agent: str,
        kind: str,
        payload: dict,
        source_agent: str = 'motto-director',
    ):
        args = {
            'target_agent': target_agent,
            'kind': kind,
            'payload': payload,
            'source_agent': source_agent,
        }
        await self.call_tool('ona-mcp___signal_intent', args)

    async def get_recent_events(self, since_minutes: int = 60, agent_name: str = None,
                                 kind: str = None) -> list[dict]:
        args: dict = {'since_minutes': since_minutes}
        if agent_name is not None:
            args['agent_name'] = agent_name
        if kind is not None:
            args['kind'] = kind
        result = await self.call_tool('ona-mcp___get_recent_events', args)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get('events', result.get('data', []))
        return []
