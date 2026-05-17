from __future__ import annotations

import logging
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


@dataclass
class CompletionResult:
    session_id: str
    success: bool
    summary: str
    artifact_id: Optional[int] = None
    error: Optional[str] = None


def _normalize_status(status: Optional[str]) -> str:
    return (status or "").strip().lower()


def _run_end_status(outcome: str) -> str:
    if outcome in SUCCESS_STATUSES:
        return "success"
    if outcome in CANCELLED_STATUSES:
        return "cancelled"
    return "error"


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

        run_end_status = _run_end_status(outcome)
        success = run_end_status == "success"
        task_title = session_meta.task_title

        try:
            output = await self._factory.get_final_output(sid)
        except Exception as exc:
            log.warning("Could not get output for session %s: %s", sid, exc)
            output = f"(output unavailable: {exc})"

        artifact_id = None
        try:
            artifact_id = await self._fleet.record_artifact(
                agent_name="motto-director",
                kind="droid_completion",
                body=output or "(empty)",
                intent=task_title,
                run_id=session_meta.run_id or None,
                meta={"session_id": sid, "outcome": outcome},
            )
        except Exception as exc:
            log.warning("record_artifact failed for %s: %s", sid, exc)

        if session_meta.run_id:
            try:
                await self._fleet.record_run_end(
                    session_meta.run_id,
                    run_end_status,
                    {"session_id": sid, "outcome": outcome, "task": task_title},
                )
            except Exception as exc:
                log.warning("record_run_end failed for run %s: %s", session_meta.run_id, exc)

        try:
            if success:
                self._store.mark_complete(sid, artifact_id=artifact_id)
            else:
                self._store.mark_failed(sid)
        except Exception as exc:
            log.warning("Failed to update session store for %s: %s", sid, exc)

        goal_id = session_meta.goal_id
        if success and goal_id and self._goals:
            goal = self._goals.get_goal(goal_id)
            if goal:
                self._goals.update_goal_status(
                    goal_id,
                    "active",
                    f"Last completed task: {task_title}",
                )

        return CompletionResult(
            session_id=sid,
            success=success,
            summary=f"Closed run as {run_end_status} (factory status={outcome})",
            artifact_id=artifact_id,
        )


async def handle_completions(
    fleet: FleetClient,
    factory: FactoryClient,
    session_store: SessionStore,
    goals: GoalStore,
):
    handler = CompletionHandler(
        fleet_client=fleet,
        factory_client=factory,
        session_store=session_store,
        goals=goals,
    )
    return await handler.scan_all()
