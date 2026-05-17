"""JSON-backed session tracker for active Factory droid sessions."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class SessionMeta:
    session_id: str
    run_id: str
    goal_id: str
    task_title: str
    prompt_summary: str  # first 200 chars of prompt
    spawned_at: str      # ISO timestamp
    status: str          # 'running' | 'completed' | 'failed'
    completed_at: Optional[str] = None
    artifact_id: Optional[int] = None
    attempt: int = 1
    max_retries: int = 3
    original_prompt: str = ''
    intent_kind: str = ''
    intent_payload: Optional[dict] = None
    intent_source_agent: str = ''
    last_error: Optional[str] = None
    previous_session_id: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "SessionMeta":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return asdict(self)


class SessionStore:
    def __init__(self, path: str = "active_sessions.json"):
        self._path = Path(path)
        self._sessions: dict[str, SessionMeta] = {}
        self.load()

    def add(self, meta: SessionMeta) -> None:
        self._sessions[meta.session_id] = meta
        self.save()

    def mark_complete(self, session_id: str, artifact_id: Optional[int] = None) -> None:
        if session_id not in self._sessions:
            raise KeyError(f"Session {session_id!r} not found")
        s = self._sessions[session_id]
        s.status = "completed"
        s.completed_at = datetime.now(timezone.utc).isoformat()
        if artifact_id is not None:
            s.artifact_id = artifact_id
        self.save()

    def mark_failed(self, session_id: str) -> None:
        if session_id not in self._sessions:
            raise KeyError(f"Session {session_id!r} not found")
        s = self._sessions[session_id]
        s.status = "failed"
        s.completed_at = datetime.now(timezone.utc).isoformat()
        self.save()

    def get_running(self) -> list[SessionMeta]:
        return [s for s in self._sessions.values() if s.status == "running"]

    def get_all(self) -> list[SessionMeta]:
        return list(self._sessions.values())

    def get(self, session_id: str) -> Optional[SessionMeta]:
        return self._sessions.get(session_id)

    def save(self) -> None:
        tmp = str(self._path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {sid: s.to_dict() for sid, s in self._sessions.items()},
                f,
                indent=2,
            )
        os.replace(tmp, self._path)

    def load(self) -> None:
        if not self._path.exists():
            self._sessions = {}
            return
        with open(self._path, encoding="utf-8") as f:
            raw = json.load(f)
        self._sessions = {sid: SessionMeta.from_dict(d) for sid, d in raw.items()}
