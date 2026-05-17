import asyncio
import logging
from datetime import datetime, timezone

from .factory_client import FactoryClient
from .fleet_client import FleetClient
from .session_store import SessionStore, SessionMeta
from .goals import GoalStore
from .completion_handler import handle_completions

log = logging.getLogger(__name__)

BATCH_SIZE = 3


async def act(
    tasks: list,
    fleet: FleetClient,
    factory: FactoryClient,
    session_store: SessionStore,
    goals: GoalStore,
):
    # 1. Heartbeat
    try:
        running_count = len(session_store.get_running())
        await fleet.heartbeat('motto-director', {
            'cycle': 'act',
            'pending_tasks': len(tasks),
            'running_sessions': running_count,
        })
    except Exception as exc:
        log.warning('Heartbeat failed: %s', exc)

    # 2. Handle completions
    completed = await handle_completions(fleet, factory, session_store, goals)
    if completed:
        log.info('Handled %d completed sessions', len(completed))

    # 3. Spawn new tasks in batches
    if not tasks:
        log.info('No tasks to spawn this cycle')
    else:
        for i in range(0, len(tasks), BATCH_SIZE):
            batch = tasks[i:i + BATCH_SIZE]
            prompts = [t['prompt'] for t in batch]

            try:
                session_ids = await factory.spawn_swarm(prompts)
            except Exception as exc:
                log.error('spawn_swarm failed for batch %d: %s', i, exc)
                continue

            for task, sid in zip(batch, session_ids):
                if not sid:
                    log.warning('Empty session_id for task %s', task.get('task_title'))
                    continue
                run_id = ''
                try:
                    run_id = await fleet.record_run_start(
                        agent_name='motto-director',
                        kind='droid_task',
                        intent=f"{task.get('goal_id')} / {task.get('task_title')}",
                    )
                except Exception as exc:
                    log.warning('record_run_start failed: %s', exc)

                meta = SessionMeta(
                    session_id=sid,
                    run_id=run_id,
                    goal_id=task.get('goal_id', ''),
                    task_title=task.get('task_title', ''),
                    prompt_summary=task.get('prompt', '')[:200],
                    spawned_at=datetime.now(timezone.utc).isoformat(),
                    status='running',
                )
                session_store.add(meta)
                log.info('Spawned session=%s goal=%s task=%s', sid, meta.goal_id, meta.task_title)

    # 4. Log cycle summary
    try:
        await fleet.record_event(
            agent_name='motto-director',
            kind='cycle_summary',
            payload={
                'spawned': len(tasks),
                'completed': len(completed),
                'running_after': len(session_store.get_running()),
            },
        )
    except Exception as exc:
        log.warning('cycle_summary event failed: %s', exc)
