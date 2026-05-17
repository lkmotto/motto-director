import json
import logging
import os
import asyncio

import anthropic

from .perceive import PerceptionBundle

log = logging.getLogger(__name__)

ANTHROPIC_KEY = os.getenv('ANTHROPIC_API_KEY', '')
IDEATE_MODEL = os.getenv('IDEATE_MODEL', 'claude-haiku-4-5')


def _build_context(perception: PerceptionBundle) -> str:
    active_goal_ids = {s.goal_id for s in perception.active_sessions if s.status == 'running'}

    goals_summary = []
    for g in perception.active_goals:
        marker = ' [DROID RUNNING]' if g['id'] in active_goal_ids else ''
        goals_summary.append(
            f"- [{g['id']}] P{g['priority']} {g['title']}{marker}: {g['description']} (repos: {', '.join(g.get('repos', []))})"
        )

    running_sessions = [
        f"  session={s.session_id[:8]} goal={s.goal_id} task={s.task_title}"
        for s in perception.active_sessions
        if s.status == 'running'
    ]

    recent_event_lines = [
        f"  [{e.get('level','info')}] {e.get('agent_name','')} / {e.get('kind','')} - {str(e.get('payload',''))[:80]}"
        for e in perception.recent_events[-20:]
    ]

    intent_lines = [
        f"  intent from={i.get('source_agent','')} kind={i.get('kind','')} payload={str(i.get('payload',''))[:80]}"
        for i in perception.open_intents
    ]

    parts = [
        'ACTIVE GOALS:',
        '\n'.join(goals_summary) or '  (none)',
        '',
        'CURRENTLY RUNNING DROIDS:',
        '\n'.join(running_sessions) or '  (none)',
        '',
        'RECENT FLEET EVENTS (last 20):',
        '\n'.join(recent_event_lines) or '  (none)',
        '',
        'OPEN INTENTS FOR DIRECTOR:',
        '\n'.join(intent_lines) or '  (none)',
    ]
    return '\n'.join(parts)


SYSTEM_PROMPT = """You are the reasoning engine of motto-director, an autonomous AI orchestrator.
Your job is to decide which tasks to spawn as Factory droid sessions right now.

Rules:
- Do NOT spawn a droid for a goal that already has a droid running.
- Prioritize goals with priority=1 over priority=2/3.
- Each task should be a specific, actionable coding/investigation prompt for a droid.
- Return ONLY valid JSON: a list of task objects with keys: goal_id, repo, task_title, prompt, priority.
- Cap at {max_droids} tasks total.
- If nothing needs doing, return an empty list [].
- The prompt field should be a detailed instruction for the droid that includes context about the goal.
"""

USER_TEMPLATE = """{context}

Based on the above state, decide what tasks to spawn now.
Return JSON array only (no markdown, no explanation):
[{{"goal_id":"...", "repo":"...", "task_title":"...", "prompt":"...", "priority":1}}]
"""


async def ideate(perception: PerceptionBundle, max_droids: int = 5) -> list:
    if not ANTHROPIC_KEY:
        log.warning('ANTHROPIC_API_KEY not set, skipping ideate')
        return []

    active_goal_ids = {s.goal_id for s in perception.active_sessions if s.status == 'running'}
    available_goals = [g for g in perception.active_goals if g['id'] not in active_goal_ids]
    running_count = sum(1 for s in perception.active_sessions if s.status == 'running')
    slots = max_droids - running_count

    if slots <= 0 or not available_goals:
        log.info('No slots available or all goals covered, skipping ideate')
        return []

    context = _build_context(perception)
    system = SYSTEM_PROMPT.format(max_droids=slots)
    user = USER_TEMPLATE.format(context=context)

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    loop = asyncio.get_event_loop()

    def _call():
        return client.messages.create(
            model=IDEATE_MODEL,
            max_tokens=2048,
            system=system,
            messages=[{'role': 'user', 'content': user}],
        )

    try:
        response = await loop.run_in_executor(None, _call)
        raw = response.content[0].text.strip()
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
        tasks = json.loads(raw)
        if not isinstance(tasks, list):
            log.warning('ideate returned non-list: %s', raw[:200])
            return []
        tasks = [t for t in tasks if t.get('goal_id') not in active_goal_ids]
        tasks.sort(key=lambda t: t.get('priority', 9))
        return tasks[:slots]
    except Exception as exc:
        log.error('ideate failed: %s', exc)
        return []
