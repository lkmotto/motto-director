import logging

from .factory_client import FactoryClient
from .fleet_client import FleetClient
from .session_store import SessionStore
from .goals import GoalStore

log = logging.getLogger(__name__)


async def handle_completions(
    fleet: FleetClient,
    factory: FactoryClient,
    session_store: SessionStore,
    goals: GoalStore,
):
    running = session_store.get_running()
    completed = []

    for session_meta in running:
        sid = session_meta.session_id
        try:
            idle = await factory.is_idle(sid)
        except Exception as exc:
            log.warning('Failed to check session %s: %s', sid, exc)
            continue

        if not idle:
            continue

        log.info('Session %s completed (goal=%s)', sid, session_meta.goal_id)
        completed.append(session_meta)

        try:
            output = await factory.get_final_output(sid)
        except Exception as exc:
            log.warning('Could not get output for session %s: %s', sid, exc)
            output = f'(output unavailable: {exc})'

        run_id = session_meta.run_id
        goal_id = session_meta.goal_id
        task_title = session_meta.task_title

        try:
            await fleet.record_artifact(
                agent_name='motto-director',
                kind='droid_completion',
                body=output or '(empty)',
                intent=task_title,
                run_id=run_id,
            )
        except Exception as exc:
            log.warning('record_artifact failed for %s: %s', sid, exc)

        if run_id:
            try:
                await fleet.record_run_end(run_id, 'success', {'session_id': sid, 'task': task_title})
            except Exception as exc:
                log.warning('record_run_end failed for run %s: %s', run_id, exc)

        if goal_id:
            goal = goals.get_goal(goal_id)
            if goal:
                goals.update_goal_status(goal_id, 'active', f'Last completed task: {task_title}')

        try:
            session_store.mark_complete(sid)
        except Exception as exc:
            log.warning('mark_complete failed for %s: %s', sid, exc)

    return completed
