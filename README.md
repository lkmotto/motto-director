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
     open issues (labels, age), and default-branch HEAD age across
     `motto-social-agent`, `motto-sdr-agent`, `motto-appraisal-pipeline`,
     `motto-appraisal-cockpit`.
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
   - `nudge_pipeline` → `POST` to the appraisal-pipeline `/tick` endpoint
   - `noop` → skipped

`director/main.py` wires the loop and emits structured JSON logs to stdout.

## Environment

| Variable | Purpose |
| --- | --- |
| `GITHUB_TOKEN` | Read repo state, file issues, merge PRs |
| `NORTHFLANK_API_TOKEN` | Read `pipeline-auto-nudge` last-run status |
| `ANTHROPIC_API_KEY` | Call Claude Opus during ideate |
| `CLAUDE_CODE_SESSION_TOKEN` | Auth for `POST https://claude.ai/api/sessions` |
| `DIRECTOR_DRY_RUN` | Set `1` to log moves without executing them |
| `NORTHFLANK_PROJECT` | Northflank project slug (default `motto`) |
| `PIPELINE_AUTO_NUDGE_JOB` | Job name (default `pipeline-auto-nudge`) |
| `APPRAISAL_PIPELINE_TICK_URL` | Override the `/tick` endpoint URL |
| `DIRECTOR_MODEL` | Override Claude model (default `claude-opus-4-7`) |

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
