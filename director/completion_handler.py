from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from .factory_client import FactoryClient
from .fleet_client import FleetClient
from .goals import GoalStore
from .session_store import SessionMeta, SessionStore

log = logging.getLogger(__name__)

SUCCESS_STATUSES = {"idle", "completed", "complete", "done", "finished", "success", "succeeded"}
FAILURE_STATUSES = {"failed", "error", "errored", "timeout", "timed_out", "aborted"}
CANCELLED_STATUSES = {"cancelled", "canceled"}
TERMINAL_STATUSES = SUCCESS_STATUSES | FAILURE_STATUSES | CANCELLED_STATUSES
DEFAULT_SUCCESS_KEYWORDS = "DONE,SUCCESS,COMPLETE,tests pass,exit 0"
DEFAULT_FAILURE_KEYWORDS = "ERROR,FAILED,blocked,exception,cannot proceed,cannot"


def _load_keywords(env_var: str, default: str) -> list[str]:
    raw = os.getenv(env_var, default)
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


SUCCESS_KEYWORDS = _load_keywords("SUCCESS_KEYWORDS", DEFAULT_SUCCESS_KEYWORDS)
FAILURE_KEYWORDS = _load_keywords("FAILURE_KEYWORDS", DEFAULT_FAILURE_KEYWORDS)


@dataclass
class CompletionResult:
    session_id: str
    success: bool
    summary: str
    artifact_id: Optional[int] = None
    error: Optional[str] = None
    retried: bool = False
    next_session_id: Optional[str] = None


def _normalize_status(status: Optional[str]) -> str:
    return (status or "").strip().lower()


def _run_end_status(outcome: str) -> str:
    if outcome in SUCCESS_STATUSES:
        return "success"
    if outcome in CANCELLED_STATUSES:
        return "cancelled"
    return "error"


def _keyword_hits(output: str, keywords: list[str]) -> list[str]:
    text = (output or "").lower()
    return [keyword for keyword in keywords if keyword in text]


def _classify_completion(output: str, outcome: str) -> tuple[bool, str]:
    failure_hits = _keyword_hits(output, FAILURE_KEYWORDS)
    if failure_hits:
        return False, f"failure keywords detected: {', '.join(failure_hits)}"

    success_hits = _keyword_hits(output, SUCCESS_KEYWORDS)
    if success_hits:
        return True, f"success keywords detected: {', '.join(success_hits)}"

    inferred = _run_end_status(outcome) == "success"
    if inferred:
        return True, f"inferred success from status={outcome}"
    return False, f"inferred failure from status={outcome}"


class CompletionHandler:
    def __init__(
        self,
        fleet_client: FleetClient,
        factory_client: FactoryClient,
        session_store: SessionStore,
        goals: Optional[GoalStore] = None,
    ):
        self._fleet = fleet_client
        self._factory = factory_client
        self._store = session_store
        self._goals = goals

    async def scan_all(self) -> list[CompletionResult]:
        results: list[CompletionResult] = []
        for session_meta in self._store.get_running():
            sid = session_meta.session_id
            try:
                status = _normalize_status(await self._factory.get_session_status(sid))
            except Exception as exc:
                log.warning("Failed to check session %s: %s", sid, exc)
                continue

            if status not in TERMINAL_STATUSES:
                continue

            result = await self.handle(session_meta, status=status)
            results.append(result)
        return results

    async def handle(self, session_meta: SessionMeta, status: Optional[str] = None) -> CompletionResult:
        sid = session_meta.session_id
        outcome = _normalize_status(status)
        if not outcome:
            try:
                outcome = _normalize_status(await self._factory.get_session_status(sid))
            except Exception as exc:
                return CompletionResult(
                    session_id=sid,
                    success=False,
                    summary="Could not resolve session outcome",
                    error=str(exc),
                )

        if outcome not in TERMINAL_STATUSES:
            return CompletionResult(
                session_id=sid,
                success=False,
                summary=f"Session still non-terminal ({outcome or 'unknown'})",
            )

        task_title = session_meta.task_title

        try:
            output = await self._factory.get_final_output(sid)
        except Exception as exc:
            log.warning("Could not get output for session %s: %s", sid, exc)
            output = f"(output unavailable: {exc})"

        success, completion_reason = _classify_completion(output, outcome)

        artifact_id = None
        try:
            artifact_id = await self._fleet.record_artifact(
                agent_name="motto-director",
                kind="droid_completion",
                body=output or "(empty)",
                intent=task_title,
                run_id=session_meta.run_id or None,
                meta={
                    "session_id": sid,
                    "outcome": outcome,
                    "completion_reason": completion_reason,
                    "attempt": session_meta.attempt,
                },
            )
        except Exception as exc:
            log.warning("record_artifact failed for %s: %s", sid, exc)

        if success:
            if session_meta.run_id:
                try:
                    await self._fleet.record_run_end(
                        session_meta.run_id,
                        "success",
                        {
                            "session_id": sid,
                            "outcome": outcome,
                            "task": task_title,
                            "completion_reason": completion_reason,
                            "attempt": session_meta.attempt,
                        },
                    )
                except Exception as exc:
                    log.warning("record_run_end failed for run %s: %s", session_meta.run_id, exc)

            source_agent = (session_meta.intent_source_agent or "").strip()
            if source_agent:
                try:
                    await self._fleet.signal_intent(
                        target_agent=source_agent,
                        kind="director.intent.completed",
                        payload={
                            "task_title": task_title,
                            "run_id": session_meta.run_id,
                            "session_id": sid,
                            "attempt": session_meta.attempt,
                            "intent_kind": session_meta.intent_kind,
                            "intent_payload": session_meta.intent_payload,
                            "summary": completion_reason,
                        },
                    )
                except Exception as exc:
                    log.warning("signal_intent completion failed for %s: %s", source_agent, exc)

            try:
                self._store.mark_complete(sid, artifact_id=artifact_id)
            except Exception as exc:
                log.warning("Failed to mark session complete for %s: %s", sid, exc)

            goal_id = session_meta.goal_id
            if goal_id and self._goals:
                goal = self._goals.get_goal(goal_id)
                if goal:
                    self._goals.update_goal_status(
                        goal_id,
                        "active",
                        f"Last completed task: {task_title}",
                    )

            return CompletionResult(
                session_id=sid,
                success=True,
                summary=f"Closed run as success ({completion_reason})",
                artifact_id=artifact_id,
            )

        max_retries = max(1, session_meta.max_retries or 1)
        retries_remaining = max_retries - max(1, session_meta.attempt)
        error_summary = completion_reason
        if retries_remaining > 0:
            try:
                from . import act
                retried_meta = await act.retry_droid(
                    session_meta=session_meta,
                    error_summary=error_summary,
                    fleet=self._fleet,
                    factory=self._factory,
                    session_store=self._store,
                )
            except Exception as exc:
                retried_meta = None
                log.warning("retry_droid failed for %s: %s", sid, exc)

            if retried_meta:
                return CompletionResult(
                    session_id=sid,
                    success=False,
                    summary=(
                        f"Retry spawned attempt {retried_meta.attempt}/{retried_meta.max_retries} "
                        f"after failure: {error_summary}"
                    ),
                    artifact_id=artifact_id,
                    retried=True,
                    next_session_id=retried_meta.session_id,
                )

        failure_summary = (
            f"Failed after {session_meta.attempt}/{max_retries} attempts. "
            f"Latest outcome={outcome}. Reason: {error_summary}"
        )
        try:
            self._store.mark_failed(sid)
        except Exception as exc:
            log.warning("Failed to mark session failed for %s: %s", sid, exc)

        if session_meta.run_id:
            try:
                await self._fleet.record_run_end(
                    session_meta.run_id,
                    "error",
                    {
                        "session_id": sid,
                        "outcome": outcome,
                        "task": task_title,
                        "attempt": session_meta.attempt,
                        "max_retries": max_retries,
                        "error": error_summary,
                    },
                )
            except Exception as exc:
                log.warning("record_run_end failed for run %s: %s", session_meta.run_id, exc)

        try:
            await self._fleet.record_artifact(
                agent_name="motto-director",
                kind="droid_failure_summary",
                body=failure_summary,
                intent=task_title,
                run_id=session_meta.run_id or None,
                meta={
                    "session_id": sid,
                    "intent_kind": session_meta.intent_kind,
                    "attempt": session_meta.attempt,
                    "max_retries": max_retries,
                },
            )
        except Exception as exc:
            log.warning("record failure summary artifact failed for %s: %s", sid, exc)

        source_agent = (session_meta.intent_source_agent or "").strip()
        if source_agent:
            try:
                await self._fleet.signal_intent(
                    target_agent=source_agent,
                    kind="director.intent.failed",
                    payload={
                        "task_title": task_title,
                        "run_id": session_meta.run_id,
                        "session_id": sid,
                        "attempt": session_meta.attempt,
                        "max_retries": max_retries,
                        "intent_kind": session_meta.intent_kind,
                        "intent_payload": session_meta.intent_payload,
                        "error": error_summary,
                    },
                )
            except Exception as exc:
                log.warning("signal_intent failure failed for %s: %s", source_agent, exc)

        return CompletionResult(
            session_id=sid,
            success=False,
            summary=failure_summary,
            artifact_id=artifact_id,
        )


async def handle_completions(
    fleet: FleetClient,
    factory: FactoryClient,
    session_store: SessionStore,
    goals: Optional[GoalStore] = None,
):
    handler = CompletionHandler(
        fleet_client=fleet,
        factory_client=factory,
        session_store=session_store,
        goals=goals,
    )
    return await handler.scan_all()
