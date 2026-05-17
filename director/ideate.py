import json
import logging
import os
import asyncio

import anthropic
import httpx

from .perceive import PerceptionBundle

log = logging.getLogger(__name__)

ANTHROPIC_KEY = os.getenv('ANTHROPIC_API_KEY', '')
IDEATE_MODEL = os.getenv('IDEATE_MODEL', 'claude-haiku-4-5')
DEEPSEEK_KEY = os.getenv('DEEPSEEK_API', os.getenv('DEEPSEEK_API_KEY', ''))
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-chat')
DEEPSEEK_BASE_URL = 'https://api.deepseek.com/v1'


def _build_shared_context(perception: PerceptionBundle) -> str:
    running_sessions = [
        f"- session={s.session_id[:8]} title={s.task_title} status={s.status} attempt={getattr(s, 'attempt', 1)}"
        for s in perception.active_sessions
        if s.status == 'running'
    ]
    event_lines = [
        f"- [{e.get('level', 'info')}] {e.get('agent_name', '')}:{e.get('kind', '')} {str(e.get('payload', ''))[:120]}"
        for e in perception.recent_events[-20:]
    ]
    parts = [
        'RUNNING DIRECTOR SESSIONS:',
        '\n'.join(running_sessions) or '(none)',
        '',
        'RECENT FLEET EVENTS (last 20):',
        '\n'.join(event_lines) or '(none)',
    ]
    return '\n'.join(parts)


def _parse_intent_payload(intent: dict) -> dict:
    payload = intent.get('payload')
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError:
            return {'task': payload}
        return {'task': payload}
    return {}


def _build_intent_context(perception: PerceptionBundle, intent: dict) -> str:
    payload = _parse_intent_payload(intent)
    task_text = payload.get('task') or payload.get('description') or payload.get('job') or ''
    specs = payload.get('specs') or payload.get('requirements') or ''
    repo = payload.get('repo') or payload.get('repository') or ''

    parts = [
        f"INTENT KIND: {intent.get('kind', '')}",
        f"SOURCE AGENT: {intent.get('source_agent', '')}",
        f"TARGET REPO: {repo}",
        f"INTENT TASK: {task_text}",
        f"INTENT SPECS: {specs}",
        '',
        _build_shared_context(perception),
    ]
    return '\n'.join(parts)


INTENT_SYSTEM_PROMPT = """You generate an execution-ready Factory droid task from one consumed fleet intent.
Return ONLY JSON with keys:
{"task_title":"...", "repo":"...", "prompt":"...", "priority":1}

Rules:
- Keep task_title concise and action-oriented.
- prompt must be detailed and include context, explicit implementation steps, constraints, and success criteria.
- If repo is missing, infer a likely repo from context and still provide one.
- Include verification instructions in prompt (tests/lint/typecheck if available).
- Output must be valid JSON only, no markdown.
"""

INTENT_USER_TEMPLATE = """{context}

Generate one task JSON object for this intent.
"""


SELF_DIRECTED_SYSTEM_PROMPT = """You are the reasoning engine of motto-director in fallback self-directed mode.
Return ONLY JSON array of task objects:
[{"goal_id":"...", "repo":"...", "task_title":"...", "prompt":"...", "priority":1}]
Cap at {max_droids} tasks and never include goals that already have a running droid.
"""

SELF_DIRECTED_USER_TEMPLATE = """{context}

Generate tasks now.
"""


def _strip_code_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('```'):
        chunks = raw.split('```')
        if len(chunks) > 1:
            raw = chunks[1]
        if raw.startswith('json'):
            raw = raw[4:]
    return raw.strip()


async def _call_anthropic(system: str, user: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    loop = asyncio.get_event_loop()

    def _sync():
        return client.messages.create(
            model=IDEATE_MODEL,
            max_tokens=2048,
            system=system,
            messages=[{'role': 'user', 'content': user}],
        )

    response = await loop.run_in_executor(None, _sync)
    return response.content[0].text.strip()


async def _call_deepseek(system: str, user: str) -> str:
    payload = {
        'model': DEEPSEEK_MODEL,
        'max_tokens': 2048,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f'{DEEPSEEK_BASE_URL}/chat/completions',
            json=payload,
            headers={'Authorization': f'Bearer {DEEPSEEK_KEY}', 'Content-Type': 'application/json'},
        )
        resp.raise_for_status()
        return resp.json()['choices'][0]['message']['content'].strip()


async def _generate_with_backends(system: str, user: str) -> str | None:
    if ANTHROPIC_KEY:
        try:
            return await _call_anthropic(system, user)
        except Exception as exc:
            log.warning('Anthropic ideate failed (%s), trying DeepSeek', exc)
    if DEEPSEEK_KEY:
        try:
            return await _call_deepseek(system, user)
        except Exception as exc:
            log.error('DeepSeek ideate failed: %s', exc)
    return None


def _fallback_task_from_intent(intent: dict) -> dict:
    payload = _parse_intent_payload(intent)
    repo = payload.get('repo') or payload.get('repository') or ''
    task = payload.get('task') or payload.get('description') or 'Execute requested intent task'
    specs = payload.get('specs') or payload.get('requirements') or ''
    prompt = (
        f"Goal: {task}\n"
        f"Repository: {repo or '(not specified)'}\n"
        f"Intent kind: {intent.get('kind', '')}\n"
        f"Source agent: {intent.get('source_agent', '')}\n"
        f"Specs:\n{specs}\n\n"
        "Implement the requested task end-to-end, run lint/typecheck/tests, and summarize outcomes."
    )
    return {
        'goal_id': payload.get('goal_id', intent.get('kind', 'intent')),
        'repo': repo,
        'task_title': str(task)[:120],
        'prompt': prompt,
        'priority': int(payload.get('priority', 1)),
        'intent_kind': intent.get('kind', ''),
        'intent_payload': payload,
        'intent_source_agent': intent.get('source_agent', ''),
    }


async def ideate(
    perception: PerceptionBundle,
    intent: dict | None = None,
    max_droids: int = 5,
    self_directed: bool = False,
) -> list:
    if intent:
        system = INTENT_SYSTEM_PROMPT
        user = INTENT_USER_TEMPLATE.format(context=_build_intent_context(perception, intent))
        raw = await _generate_with_backends(system, user)
        if not raw:
            log.warning('No LLM backend available for intent ideation, using fallback prompt')
            return [_fallback_task_from_intent(intent)]
        try:
            task = json.loads(_strip_code_fences(raw))
            if not isinstance(task, dict):
                raise ValueError('expected task object')
            payload = _parse_intent_payload(intent)
            task.setdefault('goal_id', payload.get('goal_id', intent.get('kind', 'intent')))
            task.setdefault('repo', payload.get('repo', ''))
            task.setdefault('priority', int(payload.get('priority', 1)))
            task['intent_kind'] = intent.get('kind', '')
            task['intent_payload'] = payload
            task['intent_source_agent'] = intent.get('source_agent', '')
            return [task]
        except Exception as exc:
            log.error('Intent ideate parse failed: %s | raw=%s', exc, raw[:200])
            return [_fallback_task_from_intent(intent)]

    if not self_directed:
        return []

    active_goal_ids = {s.goal_id for s in perception.active_sessions if s.status == 'running'}
    available_goals = [g for g in perception.active_goals if g.get('id') not in active_goal_ids]
    slots = max_droids - len([s for s in perception.active_sessions if s.status == 'running'])
    if slots <= 0 or not available_goals:
        return []

    goals_summary = [
        f"- [{g.get('id')}] P{g.get('priority', 9)} {g.get('title')}: {g.get('description')} (repos: {', '.join(g.get('repos', []))})"
        for g in available_goals
    ]
    context = '\n'.join([
        'AVAILABLE GOALS:',
        '\n'.join(goals_summary),
        '',
        _build_shared_context(perception),
    ])
    system = SELF_DIRECTED_SYSTEM_PROMPT.format(max_droids=slots)
    user = SELF_DIRECTED_USER_TEMPLATE.format(context=context)

    raw = await _generate_with_backends(system, user)
    if not raw:
        tasks = []
        for goal in available_goals[:slots]:
            repo = goal.get('repos', [''])[0]
            tasks.append({
                'goal_id': goal.get('id', ''),
                'repo': repo,
                'task_title': goal.get('title', 'Self-directed task'),
                'prompt': goal.get('description', ''),
                'priority': goal.get('priority', 9),
            })
        return tasks
    try:
        tasks = json.loads(_strip_code_fences(raw))
        if not isinstance(tasks, list):
            log.warning('self-directed ideate returned non-list: %s', raw[:200])
            return []
        tasks = [t for t in tasks if t.get('goal_id') not in active_goal_ids]
        tasks.sort(key=lambda t: t.get('priority', 9))
        return tasks[:slots]
    except Exception as exc:
        log.error('self-directed ideate failed: %s', exc)
        return []
