# motto-director

Self-aware autonomous orchestrator for the motto stack. Runs as a Northflank
cron job; on every tick it perceives state across the stack, ideates the next
moves with Claude Opus, and acts on the top-ranked ones.

## Loop

```
perceive → ideate → act
```

1. **perceive** (`director/perceive.py`) — collects a typed `Snapshot` from:
   - GitHub: open PRs (CI status, review state, approvals, age, labels, head SHA),
     open issues (labels, age), and default-branch HEAD age across the watch
     list (see `DEFAULT_WATCH_REPOS`; override with `WATCH_REPOS`).
   - Northflank: last-run status of the `pipeline-auto-nudge` job.
2. **ideate** (`director/ideate.py`) — sends the snapshot to Claude Opus with a
   labor-utilization-director system prompt and parses a ranked list of
   `NextMove` objects. **Every move must include explicit `intent` (1-2
   sentences explaining WHY now)** — moves without intent are dropped before
   they reach `act`.
3. **act** (`director/act.py`) — executes the top-N moves:
   - `file_issue` → GitHub REST `POST /repos/{repo}/issues`
   - `spawn_session` → `POST https://claude.ai/api/sessions` with `repo` +
     `prompt_for_claude_code`
   - `merge_pr` → GitHub REST `PUT /repos/{repo}/pulls/{n}/merge`, only if
     CI is green, approvals ≥ 1, and the PR carries the `auto-merge-ok` label
   - `compound_pr` → append a commit (the move's `code_changes`) to one
     long-lived rolling PR per repo (`director/auto/compound`); the PR body
     is a checklist of every appended move with timestamps + rationales.
     Optional native GH auto-merge — see `DIRECTOR_AUTO_MERGE` below.
   - `nudge_pipeline` → `POST` to the appraisal-pipeline `/tick` endpoint
   - `noop` → skipped

`director/main.py` wires the loop and emits structured JSON logs to stdout.

## Environment

All Northflank jobs in the motto org inherit the shared secret group
`sdr-agent-secrets`, so no per-job Doppler/secret wiring is required — the
director picks these up automatically.

Provided by `sdr-agent-secrets`:

| Variable | Purpose |
| --- | --- |
| `GITHUB_TOKEN` | Read repo state, file issues, merge PRs, append to compound PRs |
| `NORTHFLANK_API_KEY` | Read `pipeline-auto-nudge` last-run status; auth for `/tick` |
| `DEEPSEEK_API_KEY` | Primary LLM for ideate. Recommended on cost+quality. |
| `GROQ_API_KEY` | Fastest free fallback for ideate. |
| `OPENROUTER_API_KEY` | Free-tier fallback for ideate. |
| `ANTHROPIC_API_KEY` | Optional last-resort fallback. The pivot away from Anthropic happened because we ran out of API credit; only set this if you've topped it up. |
| `CLAUDE_CODE_OAUTH_TOKEN` | Canonical Claude Max OAuth token (Doppler `motto-core/prd`). When unset, `spawn_session` moves are skipped (the run logs `spawn_session.skipped` and continues). For backward compat, the legacy `CLAUDE_CODE_SESSION_TOKEN` is still read as a fallback. |

### LLM provider matrix

The default chain is **deepseek → groq → openrouter → anthropic**. On
401/402/429/5xx (or a missing key, or any unexpected error) the director
fails over to the next provider and emits `ideate.provider_failover`. On
success it emits `ideate.provider_used {provider, model, tokens_in,
tokens_out}`.

| `LLM_PROVIDER` | Default model | Base URL | Get a key |
| --- | --- | --- | --- |
| `deepseek` *(default)* | `deepseek-chat` | `https://api.deepseek.com/v1` | https://platform.deepseek.com — cheap, high-quality |
| `groq` | `llama-3.3-70b-versatile` | `https://api.groq.com/openai/v1` | https://console.groq.com — fastest free option |
| `openrouter` | `meta-llama/llama-3.3-70b-instruct:free` (overridable via `OPENROUTER_MODEL`) | `https://openrouter.ai/api/v1` | https://openrouter.ai — free tier on llama-3.3 |
| `anthropic` | `claude-opus-4-7` | n/a (Anthropic SDK) | https://console.anthropic.com — last in chain |

All three OpenAI-compatible providers go through the `openai` SDK with a
`base_url` swap.

Director-specific overrides (optional):

| Variable | Purpose |
| --- | --- |
| `LLM_PROVIDER` | Primary provider for ideate: `deepseek` (default), `groq`, `openrouter`, `anthropic`. The chain fails over through the rest in canonical order. |
| `DEEPSEEK_MODEL` / `GROQ_MODEL` / `OPENROUTER_MODEL` / `DIRECTOR_ANTHROPIC_MODEL` | Model overrides per provider. |
| `WATCH_REPOS` | Comma-separated repo slugs to perceive (e.g. `lkmotto/motto-social-agent,lkmotto/motto-sdr-agent`). Empty/unset uses `DEFAULT_WATCH_REPOS`. Set this on the Northflank job to override the default watch list. |
| `DIRECTOR_COMPOUND_BRANCH` | Long-lived branch name for the rolling compound PR per repo. Default `director/auto/compound`. |
| `DIRECTOR_COMPOUND_MAX_MOVES` | Force-flush (enable auto-merge) when the compound PR reaches this many moves. Default `10`. |
| `DIRECTOR_AUTO_MERGE` | When `true` and not in dry-run, enable native GitHub auto-merge (squash) on the compound PR after each append. CI green → GitHub merges automatically; next tick opens a fresh compound. |
| `DIRECTOR_ALLOW_SELF_MOD` | Required (`true`) for a `compound_pr` move to touch motto-director's own paths (`director/`, `tests/`, `.github/`, `Dockerfile`, `pyproject.toml`, `scripts/`). Off by default. |
| `DIRECTOR_DRY_RUN` | Set `1` to log moves without executing them |
| `NORTHFLANK_PROJECT` | Northflank project slug (default `motto`) |
| `PIPELINE_AUTO_NUDGE_JOB` | Job name (default `pipeline-auto-nudge`) |
| `APPRAISAL_PIPELINE_TICK_URL` | Override the `/tick` endpoint URL |
| `NORTHFLANK_API_TOKEN` | Legacy fallback if `NORTHFLANK_API_KEY` is unset |

## Local

```bash
uv pip install -e ".[dev]"
DIRECTOR_DRY_RUN=1 python -m director.main
pytest
ruff check .
```

## Northflank deploy

Build the image from `Dockerfile` and deploy as a **scheduled job** on a
30-minute cron (`*/30 * * * *`). Wire the env vars above into the job's secret
group. The container `CMD` is `python -m director.main`; the process exits 0
when the loop completes, which Northflank treats as a successful run.

For a safe rollout, set `DIRECTOR_DRY_RUN=1` for the first few cron runs and
inspect the structured logs before flipping it off.
