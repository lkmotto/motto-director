import logging
import os
from datetime import datetime, timezone

from .factory_client import FactoryClient
from .fleet_client import FleetClient
from .session_store import SessionStore, SessionMeta
from .completion_handler import handle_completions

log = logging.getLogger(__name__)

BATCH_SIZE = 3
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '3'))


def _extract_session_id(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ''
    sid = payload.get('id') or payload.get('session_id')
    if isinstance(sid, str):
        return sid
    data = payload.get('data')
    if isinstance(data, dict):
        sid = data.get('id') or data.get('session_id')
        if isinstance(sid, str):
            return sid
    return ''


def _build_retry_prompt(original_prompt: str, error_summary: str, attempt: int) -> str:
    return (
        f"{original_prompt.rstrip()}\n\n"
        f"Previous attempt failed with: {error_summary}\n"
        f"Fix and continue. This is retry attempt {attempt}."
    )


async def retry_droid(
    session_meta: SessionMeta,
    error_summary: str,
    fleet: FleetClient,
    factory: FactoryClient,
    session_store: SessionStore,
) -> SessionMeta | None:
    current_attempt = max(1, session_meta.attempt)
    max_retries = max(1, session_meta.max_retries or MAX_RETRIES)
    if current_attempt >= max_retries:
        return None

    next_attempt = current_attempt + 1
    base_prompt = session_meta.original_prompt or session_meta.prompt_summary
    retry_prompt = _build_retry_prompt(base_prompt, error_summary, next_attempt)

    try:
        payload = await factory.spawn_session(prompt=retry_prompt)
        session_id = _extract_session_id(payload)
    except Exception as exc:
        log.error('Retry spawn failed for session %s: %s', session_meta.session_id, exc)
        return None

    if not session_id:
        log.error('Retry spawn returned empty session id for %s', session_meta.session_id)
        return None

    try:
        session_store.mark_failed(session_meta.session_id)
    except Exception as exc:
        log.warning('Could not mark failed session before retry (%s): %s', session_meta.session_id, exc)

    retried_meta = SessionMeta(
        session_id=session_id,
        run_id=session_meta.run_id,
        goal_id=session_meta.goal_id,
        task_title=session_meta.task_title,
        prompt_summary=retry_prompt[:200],
        original_prompt=session_meta.original_prompt or base_prompt,
        spawned_at=datetime.now(timezone.utc).isoformat(),
        status='running',
        attempt=next_attempt,
        max_retries=max_retries,
        intent_kind=session_meta.intent_kind,
        intent_payload=session_meta.intent_payload,
        intent_source_agent=session_meta.intent_source_agent,
        previous_session_id=session_meta.session_id,
        last_error=error_summary,
    )
    session_store.add(retried_meta)

    try:
        await fleet.record_event(
            agent_name='motto-director',
            kind='retry_spawned',
            payload={
                'previous_session_id': session_meta.session_id,
                'session_id': session_id,
                'run_id': session_meta.run_id,
                'attempt': next_attempt,
                'max_retries': max_retries,
                'error_summary': error_summary,
            },
            run_id=session_meta.run_id or None,
        )
    except Exception as exc:
        log.warning('retry_spawned event failed: %s', exc)

    log.info(
        'Spawned retry session=%s previous_session=%s attempt=%d/%d',
        session_id,
        session_meta.session_id,
        next_attempt,
        max_retries,
    )
    return retried_meta


async def act(
    tasks: list,
    fleet: FleetClient,
    factory: FactoryClient,
    session_store: SessionStore,
    goals=None,
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
                        intent=f"{task.get('intent_kind', task.get('goal_id', 'intent'))} / {task.get('task_title')}",
                    )
                except Exception as exc:
                    log.warning('record_run_start failed: %s', exc)

                max_retries = int(task.get('max_retries', MAX_RETRIES))
                meta = SessionMeta(
                    session_id=sid,
                    run_id=run_id,
                    goal_id=task.get('goal_id', task.get('intent_kind', 'intent')),
                    task_title=task.get('task_title', ''),
                    prompt_summary=task.get('prompt', '')[:200],
                    original_prompt=task.get('prompt', ''),
                    spawned_at=datetime.now(timezone.utc).isoformat(),
                    status='running',
                    attempt=1,
                    max_retries=max_retries,
                    intent_kind=task.get('intent_kind', ''),
                    intent_payload=task.get('intent_payload'),
                    intent_source_agent=task.get('intent_source_agent', ''),
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
