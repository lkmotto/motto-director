"""Ideate: ask an LLM to rank next moves for the motto stack.

Default provider chain: deepseek → groq → openrouter → anthropic. Anthropic
is last (Anthropic API credit is the bottleneck this layer was built to dodge);
all three primary providers are OpenAI-compatible and routed through the
openai SDK with a base_url + api_key swap.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal

from anthropic import Anthropic
from openai import OpenAI

from director.perceive import Snapshot

MoveKind = Literal[
    "spawn_session",
    "file_issue",
    "merge_pr",
    "nudge_pipeline",
    "compound_pr",
    "noop",
]

_VALID_KINDS: frozenset[str] = frozenset(
    ("spawn_session", "file_issue", "merge_pr", "nudge_pipeline", "compound_pr", "noop")
)

# OpenAI-compatible providers. Each entry: (key_env, default_model_env,
# default_model, base_url).
PROVIDER_CONFIG: dict[str, dict[str, str]] = {
    "deepseek": {
        "key_env": "DEEPSEEK_API_KEY",
        "model_env": "DEEPSEEK_MODEL",
        "default_model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
    },
    "groq": {
        "key_env": "GROQ_API_KEY",
        "model_env": "GROQ_MODEL",
        "default_model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "openrouter": {
        "key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "default_model": "meta-llama/llama-3.3-70b-instruct:free",
        "base_url": "https://openrouter.ai/api/v1",
    },
}

# Chain default: claude_max first. The Claude Max OAuth token (Anthropic's
# $200/mo Pro/Max plan, used by the Claude Code CLI) is bottomless for our
# usage, while the OpenAI-compatible free tiers (deepseek/groq/openrouter)
# are aggressively rate-limited and the raw ANTHROPIC_API_KEY runs on
# pay-per-token credit. Order: subscription → free tiers → paid API.
CHAIN_ORDER: tuple[str, ...] = (
    "claude_max",
    "deepseek",
    "groq",
    "openrouter",
    "anthropic",
)
ANTHROPIC_MODEL_ENV = "DIRECTOR_ANTHROPIC_MODEL"
ANTHROPIC_DEFAULT_MODEL = "claude-opus-4-7"

# Claude Max via the official `claude` CLI binary (subprocess).
#
# As of Jan 9, 2026 Anthropic enforces server-side that subscription OAuth
# tokens are usable only inside the official Claude Code client. Direct
# /v1/messages calls with the OAuth token now return 403/429 even with the
# Claude Code identity prefix and oauth beta header (formalized in docs
# Feb 19, 2026: https://docs.anthropic.com/en/docs/claude-code/legal-and-compliance).
#
# The compliant way to use the Max subscription from a server is to install
# the `claude` CLI in the container, set CLAUDE_CODE_OAUTH_TOKEN as the
# long-lived headless-auth token (generated via `claude setup-token`), and
# shell out with `claude -p`. The CLI is the authorized client; calling it
# from a subprocess preserves subscription billing.
CLAUDE_MAX_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_MAX_MODEL_ENV = "DIRECTOR_CLAUDE_MAX_MODEL"
CLAUDE_MAX_DEFAULT_MODEL = "claude-sonnet-4-5"
CLAUDE_MAX_BIN_ENV = "CLAUDE_CLI_BIN"
CLAUDE_MAX_DEFAULT_BIN = "claude"
CLAUDE_MAX_TIMEOUT_S = 120  # CLI subprocess startup adds ~1-2s; pad generously
# Retained for backward-compat with any callers/tests; no longer used for
# transport. Kept so other modules importing these constants don't break.
CLAUDE_MAX_BETA = "oauth-2025-04-20"
CLAUDE_MAX_API_VERSION = "2023-06-01"
CLAUDE_MAX_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_CODE_SYSTEM_PREFIX = (
    "You are Claude Code, Anthropic's official CLI for Claude."
)

_FAILOVER_STATUSES: frozenset[int] = frozenset({401, 402, 429, 500, 502, 503, 504})

SYSTEM_PROMPT = """You are the labor-utilization director for the motto stack.

The motto stack consists of four repos:
- motto-social-agent: outbound social automation
- motto-sdr-agent: sales-development reps
- motto-appraisal-pipeline: backend pipeline that produces appraisals
- motto-appraisal-cockpit: human-in-the-loop UI

Your job: given a Snapshot of repo + queue state, propose a ranked list of
next moves that maximize productive throughput across the stack. Prefer moves
that unblock humans, ship merged work, or reactivate stalled automation.

Hard rules:
1. Every move MUST include explicit `intent` (1-2 sentences explaining WHY now,
   referencing concrete signals from the snapshot — PR numbers, ages, statuses).
   Moves without intent are dropped.
2. `kind` is one of: spawn_session, file_issue, merge_pr, nudge_pipeline,
   compound_pr, noop.
3. `priority` is an integer 1-5 (1 = highest).
4. `prompt_for_claude_code` is required for spawn_session moves; it must be a
   self-contained brief a fresh Claude Code session can act on.
5. For `compound_pr` moves, populate `code_changes` as a list of
   {path, content} objects with the full new file content. The director
   appends these as a single commit to a long-lived rolling PR per repo.
6. Output STRICT JSON: {"moves": [NextMove, ...]} with no prose.
"""


@dataclass
class NextMove:
    repo: str
    kind: MoveKind
    title: str
    rationale: str
    prompt_for_claude_code: str
    priority: int
    intent: str
    code_changes: list[dict[str, str]] = field(default_factory=list)


def _log(event: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


def _snapshot_to_prompt(snapshot: Snapshot) -> str:
    return json.dumps(asdict(snapshot), indent=2, default=str)


def _coerce_move(raw: dict) -> NextMove | None:
    intent = (raw.get("intent") or "").strip()
    if not intent:
        return None  # hard requirement: drop moves without intent
    kind = raw.get("kind")
    if kind not in _VALID_KINDS:
        return None
    try:
        priority = int(raw.get("priority", 5))
    except (TypeError, ValueError):
        priority = 5
    priority = max(1, min(5, priority))
    code_changes: list[dict[str, str]] = []
    raw_changes = raw.get("code_changes") or []
    if isinstance(raw_changes, list):
        for c in raw_changes:
            if isinstance(c, dict) and "path" in c and "content" in c:
                code_changes.append(
                    {"path": str(c["path"]), "content": str(c["content"])}
                )
    return NextMove(
        repo=str(raw.get("repo", "")),
        kind=kind,  # type: ignore[arg-type]
        title=str(raw.get("title", "")).strip(),
        rationale=str(raw.get("rationale", "")).strip(),
        prompt_for_claude_code=str(raw.get("prompt_for_claude_code", "")).strip(),
        priority=priority,
        intent=intent,
        code_changes=code_changes,
    )


def _provider_chain() -> list[str]:
    # Default primary is now claude_max (Claude Code OAuth) — see CHAIN_ORDER
    # comment for rationale. LLM_PROVIDER env can still pin a different one.
    primary = os.environ.get("LLM_PROVIDER", "claude_max").lower()
    rest = [p for p in CHAIN_ORDER if p != primary]
    return [primary, *rest] if primary in CHAIN_ORDER else list(CHAIN_ORDER)


@dataclass
class _ProviderResult:
    text: str
    model: str
    tokens_in: int
    tokens_out: int


class _ProviderUnavailable(Exception):
    """Provider can't be tried (e.g. missing API key)."""


class _ProviderHTTPError(Exception):
    """Provider returned an error we should fail over from."""

    def __init__(self, status_code: int | None, reason: str):
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


def _call_claude_max(system: str, user_msg: str) -> _ProviderResult:
    """Invoke the Claude Max subscription via the official `claude` CLI.

    Why subprocess and not /v1/messages directly:
        Anthropic blocks subscription OAuth tokens from the bare API as of
        Jan 9, 2026 (formalized Feb 19, 2026). The CLI is the authorized
        client; calling it from a subprocess is the supported path.

    Token plumbing:
        CLAUDE_CODE_OAUTH_TOKEN must be set in the process env. The CLI
        picks it up automatically; we don't pass it on the command line.

    Output handling:
        We invoke `claude -p <prompt> --output-format json --max-turns 1
        --append-system-prompt <system>`. The CLI emits a single JSON object
        with {result, total_cost_usd, num_turns, ...}. We do NOT use
        --system-prompt because that overrides Claude Code's own system
        prompt, which is exactly what kills subscription billing identity.
        --append-system-prompt preserves it.

    Failure modes mapped onto the chain's failover taxonomy:
        - missing token / missing binary  → _ProviderUnavailable (try next provider)
        - non-zero exit / timeout / parse → _ProviderHTTPError (treat as 502)
    """
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    if not os.environ.get(CLAUDE_MAX_TOKEN_ENV):
        raise _ProviderUnavailable(f"{CLAUDE_MAX_TOKEN_ENV} not set")
    bin_path = os.environ.get(CLAUDE_MAX_BIN_ENV, CLAUDE_MAX_DEFAULT_BIN)
    if shutil.which(bin_path) is None:
        raise _ProviderUnavailable(f"`{bin_path}` CLI not on PATH")

    model = os.environ.get(CLAUDE_MAX_MODEL_ENV, CLAUDE_MAX_DEFAULT_MODEL)
    cmd = [
        bin_path,
        "-p",
        user_msg,
        "--output-format",
        "json",
        "--max-turns",
        "1",
        "--model",
        model,
        "--append-system-prompt",
        system,
        # Northflank containers run as root. The Claude Code CLI refuses to
        # operate non-interactively under root unless this flag is set
        # (see anthropics/claude-code#3490, #2951, #9184). Safe here because
        # `--max-turns 1` means the model returns a single JSON response and
        # never invokes a tool — there are no permission prompts to bypass.
        "--dangerously-skip-permissions",
    ]
    # Companion to --dangerously-skip-permissions: the CLI also requires
    # IS_SANDBOX=1 in env to confirm the operator understands they are
    # running in a sandbox-like environment (root container, ephemeral fs).
    sub_env = {**os.environ, "IS_SANDBOX": "1"}
    # CRITICAL: strip API-key envs from the subprocess env. Per Anthropic
    # auth precedence (https://docs.anthropic.com/en/docs/claude-code/iam):
    #   1. cloud provider creds (BEDROCK/VERTEX/FOUNDRY)
    #   2. ANTHROPIC_AUTH_TOKEN  (LLM gateways)
    #   3. ANTHROPIC_API_KEY     (direct API; "In non-interactive mode (-p),
    #                              the key is always used when present.")
    #   4. apiKeyHelper
    #   5. CLAUDE_CODE_OAUTH_TOKEN  <- this is what we want
    #   6. interactive /login
    # If ANTHROPIC_API_KEY is in env (it is — `motto-core/prd` ships it for
    # the anthropic-sdk fallback in the provider chain), the CLI silently
    # routes calls through the bare API. If that key has a $0 balance the
    # CLI returns `result: "Credit balance is too low"` with tokens_in=0,
    # tokens_out=0, total_cost_usd=0, exit_code=1. We've been silently
    # producing zero moves for this exact reason. Strip the API-key envs
    # so the CLI falls through to CLAUDE_CODE_OAUTH_TOKEN (#5) and bills
    # the Max subscription instead.
    sub_env.pop("ANTHROPIC_API_KEY", None)
    sub_env.pop("ANTHROPIC_AUTH_TOKEN", None)
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_MAX_TIMEOUT_S,
            check=False,
            env=sub_env,
            # The Claude Code CLI inspects stdin even when `-p <text>` provides
            # the prompt: if stdin is open it will pause for ~3s waiting for
            # piped input, then warn and fall through. In a Northflank cron
            # the inherited stdin is a closed/orphan TTY that confuses the CLI
            # and causes a non-zero exit. Pin stdin to /dev/null so the CLI
            # treats `-p` as the sole input source.
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise _ProviderHTTPError(504, f"claude CLI timeout after {CLAUDE_MAX_TIMEOUT_S}s") from exc
    except FileNotFoundError as exc:  # pragma: no cover -- shutil.which guards this
        raise _ProviderUnavailable(f"claude CLI vanished mid-run: {exc}") from exc

    # The CLI sometimes emits a fully-formed JSON result on stdout AND exits
    # non-zero (observed under root containers + IS_SANDBOX=1). When that
    # happens, prefer parsing stdout over treating the exit code as fatal.
    stdout_text = (result.stdout or "").strip()
    parsed_body: dict | None = None
    if stdout_text:
        try:
            parsed_body = json.loads(stdout_text)
        except json.JSONDecodeError:
            parsed_body = None

    if result.returncode != 0:
        # If we got a parseable body with a `result` field, treat the exit
        # code as advisory (the run actually produced output). Log it for
        # observability but don't fail the provider.
        if parsed_body and isinstance(parsed_body, dict) and parsed_body.get("result"):
            _log(
                "claude_max.advisory_exit",
                exit_code=result.returncode,
                terminal_reason=parsed_body.get("terminal_reason"),
                permission_denials=len(parsed_body.get("permission_denials") or []),
            )
        else:
            err_tail = (result.stderr or "").strip()[-400:]
            out_head = stdout_text[:600]
            out_tail = stdout_text[-600:]
            raise _ProviderHTTPError(
                502,
                (
                    f"claude CLI exit={result.returncode} "
                    f"stderr={err_tail!r} "
                    f"stdout_head={out_head!r} "
                    f"stdout_tail={out_tail!r}"
                ),
            )

    if parsed_body is None:
        raise _ProviderHTTPError(
            502, f"claude CLI bad JSON: {stdout_text[:600]}"
        )

    text = parsed_body.get("result") or ""
    usage = parsed_body.get("usage") or {}
    # Diagnostic: the CLI JSON shape has shifted across versions. Log the
    # top-level keys + usage keys + result text length so we can see why
    # tokens_in/tokens_out come back as 0 and confirm `result` is populated.
    _log(
        "claude_max.body_shape",
        body_keys=sorted(parsed_body.keys()),
        usage_keys=sorted(usage.keys()) if isinstance(usage, dict) else [],
        usage_value=usage if isinstance(usage, dict) else None,
        result_text_len=len(text),
        result_text_head=text[:300],
        terminal_reason=parsed_body.get("terminal_reason"),
        num_turns=parsed_body.get("num_turns"),
        total_cost_usd=parsed_body.get("total_cost_usd"),
    )
    # Some CLI versions nest token counts under message.usage instead of
    # the top-level usage key. Fall back to that shape if needed.
    if not usage and isinstance(parsed_body.get("message"), dict):
        msg_usage = parsed_body["message"].get("usage") or {}
        if isinstance(msg_usage, dict):
            usage = msg_usage
    return _ProviderResult(
        text=text,
        model=parsed_body.get("model", model),
        tokens_in=usage.get("input_tokens", 0) if isinstance(usage, dict) else 0,
        tokens_out=usage.get("output_tokens", 0) if isinstance(usage, dict) else 0,
    )


def _call_anthropic(client: Anthropic, system: str, user_msg: str) -> _ProviderResult:
    model = os.environ.get(ANTHROPIC_MODEL_ENV, ANTHROPIC_DEFAULT_MODEL)
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )
    usage = getattr(response, "usage", None)
    return _ProviderResult(
        text=text,
        model=model,
        tokens_in=getattr(usage, "input_tokens", 0) if usage else 0,
        tokens_out=getattr(usage, "output_tokens", 0) if usage else 0,
    )


def _call_openai_compatible(provider: str, system: str, user_msg: str) -> _ProviderResult:
    cfg = PROVIDER_CONFIG[provider]
    api_key = os.environ.get(cfg["key_env"])
    if not api_key:
        raise _ProviderUnavailable(f"{cfg['key_env']} not set")
    model = os.environ.get(cfg["model_env"], cfg["default_model"])
    client = OpenAI(api_key=api_key, base_url=cfg["base_url"], max_retries=0)
    response = client.chat.completions.create(
        model=model,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
    )
    text = response.choices[0].message.content or ""
    usage = response.usage
    return _ProviderResult(
        text=text,
        model=model,
        tokens_in=getattr(usage, "prompt_tokens", 0) if usage else 0,
        tokens_out=getattr(usage, "completion_tokens", 0) if usage else 0,
    )


def _try_provider(provider: str, system: str, user_msg: str) -> _ProviderResult:
    if provider == "claude_max":
        # _call_claude_max raises _ProviderUnavailable / _ProviderHTTPError
        # directly with accurate status codes — no extra wrapping.
        return _call_claude_max(system, user_msg)
    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise _ProviderUnavailable("ANTHROPIC_API_KEY not set")
        try:
            return _call_anthropic(Anthropic(), system, user_msg)
        except Exception as exc:  # noqa: BLE001
            status = _extract_status(exc)
            raise _ProviderHTTPError(status, str(exc)) from exc
    if provider in PROVIDER_CONFIG:
        try:
            return _call_openai_compatible(provider, system, user_msg)
        except _ProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            status = _extract_status(exc)
            raise _ProviderHTTPError(status, str(exc)) from exc
    raise _ProviderUnavailable(f"unknown provider {provider}")


def _extract_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        if response is not None:
            status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def ideate(snapshot: Snapshot, *, client: Anthropic | None = None) -> list[NextMove]:
    """Call an LLM and return a ranked list of NextMove objects.

    If `client` is provided, use it directly (test override; Anthropic-shaped).
    Otherwise walk the provider chain (LLM_PROVIDER first; default 'deepseek'),
    failing over on missing keys, 401/402/429/5xx, or other errors.
    """
    user_msg = (
        "Snapshot of the motto stack:\n\n```json\n"
        + _snapshot_to_prompt(snapshot)
        + "\n```\n\nPropose the ranked next moves as strict JSON."
    )

    if client is not None:
        result = _call_anthropic(client, SYSTEM_PROMPT, user_msg)
        return _parse_text_to_moves(result.text)

    chain = _provider_chain()
    for i, provider in enumerate(chain):
        next_provider = chain[i + 1] if i + 1 < len(chain) else None
        try:
            result = _try_provider(provider, SYSTEM_PROMPT, user_msg)
        except _ProviderUnavailable as exc:
            _log(
                "ideate.provider_failover",
                **{
                    "from": provider,
                    "to": next_provider,
                    "status_code": None,
                    "reason": str(exc),
                },
            )
            continue
        except _ProviderHTTPError as exc:
            _log(
                "ideate.provider_failover",
                **{
                    "from": provider,
                    "to": next_provider,
                    "status_code": exc.status_code,
                    "reason": exc.reason,
                },
            )
            # Spec says fail over on 401/402/429/5xx; for other status codes we
            # also fail over (treating them as transient/unrecognized) since
            # the alternative is a dead loop.
            if exc.status_code is not None and exc.status_code not in _FAILOVER_STATUSES:
                # 4xx other than auth/payment/rate-limit — likely a permanent
                # request error; still failover to avoid sticking on a bad
                # provider, but we noted it via the failover log already.
                pass
            continue

        _log(
            "ideate.provider_used",
            provider=provider,
            model=result.model,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            response_text_len=len(result.text or ""),
            response_text_head=(result.text or "")[:500],
        )
        moves = _parse_text_to_moves(result.text)
        # Diagnostic: when zero moves are produced, log enough of the raw
        # response to debug whether the model returned `{"moves":[]}`,
        # malformed JSON, a refusal, or wrapped its output unexpectedly.
        if not moves:
            _log(
                "ideate.zero_moves",
                provider=provider,
                model=result.model,
                response_text_len=len(result.text or ""),
                response_text_full=(result.text or "")[:4000],
                prompt_user_msg_len=len(user_msg),
            )
        return moves

    return []


def _parse_text_to_moves(text: str) -> list[NextMove]:
    payload = _extract_json(text)
    raw_moves = payload.get("moves", []) if isinstance(payload, dict) else []
    moves: list[NextMove] = []
    for raw in raw_moves:
        if not isinstance(raw, dict):
            continue
        move = _coerce_move(raw)
        if move is not None:
            moves.append(move)
    moves.sort(key=lambda m: m.priority)
    return moves


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        # strip code fences
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # last-resort: try to locate a JSON object
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return {}
