# Cron migration: PPLX Computer → Northflank

The director loop used to be driven by a Perplexity Computer scheduled
task that pinged the agent on a cadence.  This document covers the
move to **native Northflank cron jobs**, which lets director run on
its own schedule independent of any external scheduler.

## What's running

Defined in [`northflank/crons.yaml`](../northflank/crons.yaml):

| Job              | Schedule (UTC)   | Command                      |
|------------------|------------------|------------------------------|
| director-perceive| `*/15 * * * *`   | `director.cli perceive`      |
| director-ideate  | `5 * * * *`      | `director.cli ideate`        |
| director-act     | `10 * * * *`     | `director.cli act`           |
| director-digest  | `15 */6 * * *`   | `director.cli digest`        |
| director-meta    | `0 9 * * 1`      | `director.cli meta`          |

All jobs share the same Docker image (the one built from this repo's
`Dockerfile`) and read state from the shared Postgres control-plane
plus the existing `director:cycle` lock — overlapping ticks short-
circuit safely.

## How it gets applied

1. Edit `northflank/crons.yaml`.
2. Open a PR.  CI (`pytest`) validates that the manifest parses and
   `apply_northflank_crons.py` produces the expected idempotent calls.
3. Merge to `main`.
4. The [`apply-crons` GitHub Action](../.github/workflows/apply-crons.yaml)
   detects the path change, runs `scripts/apply_northflank_crons.py`
   with `NORTHFLANK_API_KEY` (org-level secret), and:
   - **PATCHes** any cron job that already exists with the same name,
   - **POSTs** any cron that's new.
5. The script prints `<name>\t<action>` per cron for the audit log.

You can also run the workflow manually with `workflow_dispatch` and a
`dry_run` input that prints intended writes without touching the API.

## Cutover

1. Land this PR + the cron-job rows it provisions.
2. Verify the first scheduled tick runs cleanly:
   ```
   gh run list --repo lkmotto/motto-director --workflow apply-crons --limit 1
   ```
   then in Northflank UI: project `motto-agents` → Jobs → confirm five
   `director-*` cron rows with `Last execution` recent.
3. Disable the Perplexity Computer scheduled task that used to ping
   the director.  No code change needed — the old endpoint just stops
   getting called.
4. Watch one full hour of director-perceive ticks via the
   `motto-mcp-server` fleet endpoint or directly in Postgres
   (`SELECT * FROM director_runs ORDER BY started_at DESC LIMIT 20`).

## Rollback

If Northflank crons misbehave:

1. Pause the offending job in the Northflank UI (Jobs → `director-*` →
   Pause).  This is non-destructive — no rows are deleted.
2. Re-enable the PPLX Computer scheduled task as the temporary
   heartbeat.
3. Open an issue on `motto-director` with the cron name + the
   offending run's logs.

To roll back the entire migration, revert the PR; the `apply-crons`
workflow does not support deletes (by design — manual pause is safer
than auto-delete on revert).

## Operational notes

- **Auth**: the script uses the same precedence as
  `director.perceive.northflank_api_key()` —
  `NORTHFLANK_API_KEY`, falling back to `NORTHFLANK_API_TOKEN`.
- **Project override**: `NORTHFLANK_PROJECT` env var beats the
  `project:` field in the manifest.  Useful for staging.
- **Dry-run**: `DRY_RUN=1` prints intended payloads but never calls
  the API.  Used by tests and the manual `workflow_dispatch` trigger.
- **No deletes**: the script only creates or updates.  To remove a
  cron job permanently, drop it from `crons.yaml` AND delete it in
  the Northflank UI in the same PR.

## Why this is safe to merge with `auto-merge-ok`

This change adds files only and doesn't touch the protected modules
(`policy.py`, `observability.py`, `fleet.py`, `perceive.py`,
`ideate.py`, `act.py`, `digest.py`).  The apply script's behavior is
fully covered by `tests/test_apply_crons.py` with mocked API calls,
so no real Northflank state is mutated by CI.  Once merged, the
`apply-crons` workflow runs on the next push that touches the
manifest path.
