# motto-director — CLAUDE.md

## Purpose
Self-aware autonomous orchestrator for the motto stack. Runs as a Northflank cron job every 30 minutes. On each tick it:
1. **Perceives** — collects a typed `Snapshot` (open PRs, issues, Northflank job status) via `director/perceive.py`
2. **Ideates** — sends the snapshot to Claude Opus and parses ranked `NextMove` objects via `director/ideate.py`
3. **Acts** — executes top-N moves (`file_issue`, `spawn_session`, `merge_pr`, `nudge_pipeline`, `noop`) via `director/act.py`

## Running Locally
```bash
pip install -e .

# Dry run (no side effects, logs only)
DIRECTOR_DRY_RUN=1 python -m director.main

# Live run
python -m director.main
```

## Required Environment Variables
| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Claude Opus API key for ideation |
| `GITHUB_TOKEN` | PAT with repo + workflow scopes |
| `NORTHFLANK_API_KEY` | Northflank API key for job status |
| `NORTHFLANK_PROJECT_ID` | Northflank project containing motto services |
| `DIRECTOR_DRY_RUN` | Set to `1` to skip all side-effecting actions |
| `DIRECTOR_TOP_N` | Max moves to execute per tick (default: 3) |

## Reading Observability Output
All output is structured JSON to stdout. Each line is a complete JSON object:
```json
{"ts": "2026-05-02T14:00:00+00:00", "event": "director.start"}
{"ts": "...", "event": "director.perceived", "repos": 6, "open_prs": 4, "open_issues": 5}
{"ts": "...", "event": "director.ideated", "moves": 3}
{"ts": "...", "event": "director.acted", "move": "spawn_session", "repo": "motto-sdr-agent"}
{"ts": "...", "event": "director.done", "elapsed_s": 12.4}
```

In Northflank, filter logs by `event` field. Key events to watch:
- `director.start` / `director.done` — confirms cron fired
- `director.error` — something went wrong, check `error` field
- `director.acted` — what move was executed and on which repo

## Repo Structure
```
director/
  main.py        # entrypoint: perceive → ideate → act loop
  perceive.py    # builds Snapshot from GitHub + Northflank APIs
  ideate.py      # sends Snapshot to Claude Opus, returns NextMove list
  act.py         # executes moves with dry-run guard
tests/           # pytest unit tests
.github/workflows/ci.yml  # lint (ruff) + test + failure-comment on PR
Dockerfile       # used by Northflank cron job
pyproject.toml   # deps: anthropic, PyGithub, httpx, ruff
```

## CI
PR checks: ruff lint + pytest. On failure, a sticky comment is posted to the PR with the last 250 log lines so Claude Code sessions can diagnose without needing gh CLI access.

## Common Debugging
- **Cron not firing**: Check Northflank job status in the dashboard; verify `NORTHFLANK_PROJECT_ID` is correct
- **Ideation returning no moves**: Check `ANTHROPIC_API_KEY` is valid; run with `DIRECTOR_DRY_RUN=1` and inspect `director.ideated` log line
- **Act failing**: Check `GITHUB_TOKEN` scopes (needs `repo` + `workflow`)
