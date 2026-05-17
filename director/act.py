import asyncio
import logging

from .factory_client import FactoryClient
from .fleet_client import FleetClient
from .session_store import SessionStore
from .goals import GoalStore
from .completion_handler import handle_completions

log = logging.getLogger(__name__)

BATCH_SIZE = 3  # max concurrent spawns per act cycle


async def act(
    tasks: list[dict],
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

    # 2. Handle completions before spawning new work
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

            # Register each session
            for task, sid in zip(batch, session_ids):
                if not sid:
                    log.warning('Empty session_id returned for task %s', task.get('task_title'))
                    continue
                run_id = None
                try:
                    run_id = await fleet.record_run_start(
                        agent_name='motto-director',
                        kind='droid_task',
                        intent=f"{task.get('goal_id')} / {task.get('task_title')}",
                    )
                except Exception as exc:
                    log.warning('record_run_start failed: %s', exc)

                session_store.add(
                    session_id=sid,
                    goal_id=task.get('goal_id', ''),
                    repo=task.get('repo', ''),
                    task_title=task.get('task_title', ''),
                    run_id=run_id,
                )
                log.info('Spawned droid session=%s goal=%s task=%s', sid, task.get('goal_id'), task.get('task_title'))

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
