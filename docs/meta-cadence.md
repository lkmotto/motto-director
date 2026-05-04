# Director-meta cadence

A *third* Northflank cron, separate from the cycle cron (`cadence.md`) and the
morning digest (`digest-cadence.md`). Runs once a week to look back over the
director's recent outcomes and file advisory PRs against motto-director's own
policy logic and skill files.

## Schedule

```
0 13 * * 0
```

Sunday 13:00 UTC = **8:00 AM Central Time on Sunday**. Lands after the morning
digest so Luke can read both back-to-back.

## Northflank service config

- **Type:** Cron job
- **Image:** same as the main `motto-director` cron
- **Command:** `motto-director-meta`
- **Cron expression:** `0 13 * * 0`

Apply manually via the Northflank UI — Luke handles infra changes.

## Required env vars

Everything the cycle cron already has:

| Variable | Purpose |
| --- | --- |
| `GITHUB_TOKEN` | Read recent PRs in watched repos; create the meta-PR + branch |
| `MOTTO_MCP_URL` + `MOTTO_MCP_AUTH_TOKEN` | Read fleet decisions/events |
| At least one LLM key (`DEEPSEEK_API_KEY` / `GROQ_API_KEY` / `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY`) | Synthesize improvements |
| `META_WINDOW_DAYS` (optional, default 7) | Look-back window |

If any of these is missing, the corresponding stage no-ops and the cron exits
0 — failures here are non-fatal.

## What the meta cron does

1. **Gather outcomes** — reads fleet `recent_events` for the last
   `META_WINDOW_DAYS`, finds every `decision` with
   `choice="spawned_claude_session"`, then fetches the resulting PR's state
   via the GitHub API. Buckets each session as success (PR merged), failure
   (PR closed unmerged or reverted), or open.
2. **Synthesize improvements** — sends the outcomes JSON to the same provider
   chain `ideate.py` uses (deepseek → groq → openrouter → anthropic) with a
   meta-prompt asking for 1-3 small, high-confidence edits to either
   `director/policy.py` or `skills/*.md`. Confidence < 0.7 is dropped; max 3
   improvements per run.
3. **File one advisory PR** — opens a draft PR against motto-director itself
   carrying all surviving improvements as a body summary. Labeled
   `director-meta` and **never** `auto-merge-ok`. Luke reviews and applies the
   `proposed_change` blocks by hand.

## Why advisory, not direct edits

The LLM's `proposed_change` is a diff *suggestion* with calibrated confidence,
not a verified patch. Applying it directly would mean the director silently
edits its own brain on a weekly schedule, which violates the self-mod guard
philosophy in `compound.py`. The meta cron surfaces; Luke decides.

## Operational notes

- Read-only against fleet state; one PR per run (or zero, if no high-confidence
  improvements). Safe alongside the cycle cron without locking — never touches
  `director:cycle`.
- `director-meta`-labeled PRs are excluded from auto-merge by virtue of
  missing `auto-merge-ok`, which the cycle cron's merge gate requires.
- Empty LLM response (`{"improvements": []}`) → cron logs `meta.done
  improvements=0 pr_url=None` and exits cleanly without touching GitHub.
