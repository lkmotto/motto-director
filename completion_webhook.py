"""
FastAPI webhook server for director completion events.

Endpoints:
    POST /webhook/factory/completion  {session_id, status}
    GET  /health
    GET  /sessions/active
    GET  /sessions/all
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from director.completion_handler import CompletionHandler
from director.factory_client import FactoryClient
from director.fleet_client import FleetClient
from director.session_store import SessionStore

SESSIONS_FILE = os.getenv(
    "SESSIONS_FILE",
    str(Path(__file__).resolve().parent / "active_sessions.json"),
)

app = FastAPI(title="Director Completion Webhook")

# Lazy singletons — instantiated on first request to avoid env-var errors at import
_store: Optional[SessionStore] = None
_handler: Optional[CompletionHandler] = None


def _get_store() -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore(path=SESSIONS_FILE)
    return _store


def _get_handler() -> CompletionHandler:
    global _handler
    if _handler is None:
        _handler = CompletionHandler(
            fleet_client=FleetClient(),
            factory_client=FactoryClient(),
            session_store=_get_store(),
        )
    return _handler


# ── Request / response models ──────────────────────────────────────────────────

class CompletionPayload(BaseModel):
    session_id: str
    status: str  # 'completed' | 'failed' | ...


class CompletionResponse(BaseModel):
    session_id: str
    success: bool
    summary: str
    artifact_id: Optional[int] = None
    error: Optional[str] = None


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/sessions/active")
async def sessions_active():
    store = _get_store()
    store.load()  # refresh from disk
    return {"sessions": [s.to_dict() for s in store.get_running()]}


@app.get("/sessions/all")
async def sessions_all():
    store = _get_store()
    store.load()
    return {"sessions": [s.to_dict() for s in store.get_all()]}


@app.post("/webhook/factory/completion", response_model=CompletionResponse)
async def factory_completion(payload: CompletionPayload):
    store = _get_store()
    store.load()

    meta = store.get(payload.session_id)
    if meta is None:
        raise HTTPException(
            status_code=404,
            detail=f"Session {payload.session_id!r} not found in store",
        )

    if meta.status != "running":
        # Already processed — return cached result
        already_success = meta.status == "completed"
        return CompletionResponse(
            session_id=meta.session_id,
            success=already_success,
            summary=f"Already {meta.status}",
            artifact_id=meta.artifact_id,
            error=None if already_success else f"session status={meta.status}",
        )

    handler = _get_handler()
    result = await handler.handle(meta, status=payload.status)

    return CompletionResponse(
        session_id=result.session_id,
        success=result.success,
        summary=result.summary,
        artifact_id=result.artifact_id,
        error=result.error,
    )
