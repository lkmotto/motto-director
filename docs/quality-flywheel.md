# Quality Flywheel (director-meta v2)

Sibling to `director.meta`. Where `meta` asks an LLM for advisory PRs, the
flywheel uses **deterministic** signal-driven heuristics across three sources
to drive director self-improvement, then turns high-confidence cheap fixes
into auto-mergeable PRs.

## How it runs

Console script wired in `pyproject.toml`:

```
motto-director-quality = "director.quality_cron:run_quality"
```

Designed to run on a weekly Northflank cron. Set
`DIRECTOR_QUALITY_FLYWHEEL_ENABLED=0` in the secret group to kill-switch it.

## Signal sources

| Source | File | Configured by |
|---|---|---|
| Langfuse traces (per-tool latency p95, error rate, cost, error chains) | `director/quality/langfuse_signals.py` | `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` |
| Neon control plane (task success, decisions overturned, repeated failures) | `director/quality/postgres_signals.py` | `NEON_DATABASE_URL` or `DATABASE_URL` |
| GitHub PR outcomes across the fleet (merge rate, time-to-merge, reverts, CI flake) | `director/quality/github_signals.py` | `GITHUB_TOKEN` or `GH_TOKEN` + `gh` CLI on PATH |

Each collector returns a stable-shape dict and **no-ops gracefully** when its
source isn't configured — the cron always produces *some* report.

## Synthesizer

`director/quality/synthesizer.py` is rule-based, no LLM. Detectors:

- **High error rate**: tool with `error_rate >= 10%` → `config_tweak` (cheap, retry+backoff)
- **Slow p95**: tool with `p95 >= 30s` → `config_tweak` (cheap, timeout bump to 2× p95)
- **Repeated failures (Neon)**: same `(tool, error)` ≥ 3 times in 7d → `flag_for_review` (expensive)
- **High overturn rate**: meta overturning ≥ 20% of ideate decisions → `prompt_edit` (cheap)
- **Low merge rate**: fleet merge_rate < 50% with ≥ 5 PRs → `flag_for_review` (expensive)
- **Reverts**: any reverted merged PR → `flag_for_review` (expensive, never auto-fixable)

Returns a `QualityReport(top_problems, suggested_fixes, confidence_score, signals)`.

## PR generator

`director/quality/pr_generator.py` turns each accepted fix into either a PR or
an issue. Hard rails (top-to-bottom):

1. **Confidence threshold gate** (`QUALITY_FLYWHEEL_CONFIDENCE_THRESHOLD`,
   default 0.7).
2. **Protected-file refusal**: any payload that *would* touch
   `policy.py | observability.py | fleet.py | perceive.py | ideate.py |
   act.py | digest.py | .github/workflows/* | scripts/apply_northflank_crons.py`
   is downgraded to a tracking issue. The pattern set mirrors
   `.github/workflows/auto-merge.yaml`'s `PROTECTED_PATTERNS` 1:1.
3. **Per-run cap** (`MAX_FIXES_PER_RUN = 3`) so a single bad week never floods
   the PR queue.

**The flywheel never edits director's modules.** Cheap fixes write additive
config only:

| Fix kind | File written |
|---|---|
| `config_tweak` | `director/config/tool_overrides.yaml` |
| `prompt_edit` | `director/config/prompts/ideate_addendum.md` |
| `flag_for_review` | (issue only — no file write) |

Director consumes these overrides on its next cycle without any
protected-module edits. PRs are labelled `auto-merge-ok` + `director-flywheel`,
so the auto-merge action ships them once CI is green.

## Persistence

`migrations/0004_quality_reports.sql` adds an additive `quality_reports` table.
Each run inserts:

- `signals` JSONB
- `top_problems` JSONB
- `suggested_fixes` JSONB
- `confidence_score` float
- `pr_urls` / `issue_urls` JSONB

Trend `confidence_score` over time to track flywheel health and the
recurrence of top problems across runs.

## Telegram digest

After persistence, `director.digest.send_telegram` posts a short digest to
fleet chat. No-ops gracefully when `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
aren't set.

## Local smoke

```bash
DRY_RUN=1 python -m director.quality_cron
```

This collects whatever signals the local environment can reach, synthesizes,
and returns dry-run PR/issue URLs without touching GitHub.

## Why this is safe to ship behind auto-merge

- Never edits director's protected modules (mirrors auto-merge guard regex).
- Capped at 3 PRs per run.
- Confidence threshold gate.
- Every cheap fix is a single additive YAML/MD entry — easy diff to review,
  trivial to revert.
- Expensive fixes never become PRs.
- Each run inserts a `quality_reports` row, so the trend is observable
  even when the flywheel produces no output.
