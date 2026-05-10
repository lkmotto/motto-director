# AGENTS.md

> Operational spec for autonomous coding agents (Factory Droid, Codex, Cursor, Aider). Human-readable too.

## Identity
- **Repo:** `lkmotto/motto-director`
- **Purpose:** Self-aware autonomous orchestrator that runs perceive→ideate→act loops every 15 minutes to drive the entire motto stack forward.
- **Status:** Active on Northflank — last commit 2026-05-06
- **Owner:** Luke Motto (`ljm32901@gmail.com`)
- **Linear team:** Mottoappraisal (MOT) · project Fleet Operations

## What this code does
Northflank cron job that on every tick: perceives state (open PRs, issues, Northflank job status) across the watch-list repos; ideates with DeepSeek/Groq/OpenRouter/Anthropic to produce ranked `NextMove` objects; and acts by filing issues, spawning Claude sessions, merging PRs, appending to compound PRs, or nudging the pipeline. Every move requires an explicit `intent` rationale. Structured JSON logs emitted to stdout. Fleet awareness fed by `motto-mcp-server`.

## Architecture at a glance
- `director/main.py` — Entrypoint; wires the perceive→ideate→act loop
- `director/perceive.py` — Builds `Snapshot` from GitHub + Northflank APIs
- `director/ideate.py` — Sends Snapshot to LLM, parses `NextMove` list; multi-provider failover chain
- `director/act.py` — Executes moves with dry-run guard
- `director/cli.py` — CLI entrypoint (`python -m director.cli perceive`)
- `northflank/crons.yaml` — Cron job definitions (every 15 min)
- `scripts/` — Northflank cron apply script
- `migrations/` — DB migrations
- `Dockerfile` — Container image

## Runtime
- **Language/runtime:** Python 3.x
- **Entry point:** `python -m director.main` or `python -m director.cli perceive`
- **Hosting:** Northflank cron jobs in project `motto-agents` (image: `motto-director`)
- **Schedule:** `*/15 * * * *` (every 15 minutes, UTC)

## Required environment variables
| Variable | Purpose | Source |
|---|---|---|
| `GITHUB_TOKEN` | Read repos, file issues, merge PRs, append compound PRs | `sdr-agent-secrets` Northflank group |
| `NORTHFLANK_API_KEY` | Read `pipeline-auto-nudge` job status | `sdr-agent-secrets` Northflank group |
| `NORTHFLANK_PROJECT_ID` | Northflank project ID for job status reads | `sdr-agent-secrets` Northflank group |
| `DEEPSEEK_API_KEY` | Primary LLM for ideate (default provider) | `sdr-agent-secrets` Northflank group |
| `GROQ_API_KEY` | Fastest free fallback for ideate | `sdr-agent-secrets` Northflank group |
| `OPENROUTER_API_KEY` | Free-tier fallback (llama-3.3-70b) | `sdr-agent-secrets` Northflank group |
| `ANTHROPIC_API_KEY` | Last-resort fallback (credits may be low) | `sdr-agent-secrets` Northflank group |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Max OAuth for `spawn_session` moves | Doppler `motto-core/prd` |
| `MOTTO_MCP_URL` | Fleet MCP endpoint for perceive step | Doppler `motto-core/prd` |
| `MOTTO_MCP_AUTH_TOKEN` | Bearer token for MCP | Doppler `motto-core/prd` |
| `LANGFUSE_PUBLIC_KEY` | Langfuse OTel tracing (optional) | Doppler `motto-core/prd` |
| `LANGFUSE_SECRET_KEY` | Langfuse OTel tracing (optional) | Doppler `motto-core/prd` |
| `DIRECTOR_DRY_RUN` | Set to `1` to skip side-effecting actions | local dev |
| `DIRECTOR_TOP_N` | Max moves to execute per tick (default: 3) | Northflank env |
| `WATCH_REPOS` | Comma-separated repo slugs to perceive (override default list) | Northflank env |
| `LLM_PROVIDER` | Primary provider: `deepseek` (default), `groq`, `openrouter`, `anthropic` | Northflank env |

## Doppler config
- Project: `motto-core`
- Config: `prd`
- Pull command: `doppler run --project motto-core --config prd -- <command>`

## How to run locally
```bash
pip install -e .
# Dry run (no side effects)
DIRECTOR_DRY_RUN=1 python -m director.main
# Live run
python -m director.main
```

## How to deploy
Push to `main` → CI builds Docker image → Northflank cron jobs auto-update. Apply cron changes via `scripts/apply_northflank_crons.py` (triggered by `apply-crons` GitHub Action on `northflank/crons.yaml` changes).

## Conventions
- Branch from `main`. PRs only. No direct pushes to main.
- Use DeepSeek V4 / Reasoner for code generation. Claude is banned from this fleet for cost reasons.
- One PR per logical change. Keep diffs minimal.
- Update this AGENTS.md if you change the architecture.

## Known issues / open loops
- `ANTHROPIC_API_KEY` is last in the chain — credits may be exhausted; only use if topped up.
- `spawn_session` moves silently skip if `CLAUDE_CODE_OAUTH_TOKEN` is unset (logs `spawn_session.skipped`).
- Compound PR feature: one rolling PR per repo under `director/auto/compound` branch.
- `DIRECTOR_AUTO_MERGE` controls native GH auto-merge on compound PRs — check setting before enabling.

## Maritime status
Maritime.sh is dead. This repo does not reference Maritime.
