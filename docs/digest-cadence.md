# Morning digest cadence

Runs as a *separate* Northflank cron from the main director cycle. The cycle
cron (every 15-30 min, see `cadence.md`) handles perceive→ideate→act; this
cron runs once a day to summarize the previous 24 hours.

## Schedule

```
0 12 * * *
```

12:00 UTC = **7:00 AM Central Time** (CDT in summer, 6:00 AM CT in winter — close
enough; the digest is informational, not time-critical).

## Northflank service config

- **Service type:** Cron job
- **Image:** same as the main `motto-director` cron (already built)
- **Command:** `motto-director-digest`
- **Cron expression:** `0 12 * * *`
- **Region:** match the main director (latency to Neon + GitHub doesn't matter
  here, but logs colocate)

Apply manually via the Northflank UI — Luke handles infra changes.

## Required env vars

| Variable | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Bot API token from @BotFather |
| `TELEGRAM_CHAT_ID` | Luke's chat ID (DM the bot once, then `getUpdates`) |
| `MOTTO_MCP_URL` + `MOTTO_MCP_AUTH_TOKEN` | Read fleet runs/events for the digest body |
| `GITHUB_TOKEN` | perceive() needs it to count open PRs / awaiting review |
| `WATCH_REPOS` (optional) | Override the default watch list — same semantics as the cycle cron |
| `DIGEST_WINDOW_HOURS` (optional, default 24) | Look-back window for the summary |

If `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` is unset, the digest builds and
logs but doesn't send — safe for staging environments.

## What the digest contains

- Count of PRs merged in the last 24h (with links)
- Count of PRs awaiting Luke's review (with links)
- Count of Claude Code sessions spawned + success/failure split
- "Needs your judgment" list for PRs with failed CI, requested changes, or
  staleness > 48h
- Burn line: total runs, total events, approximate cost in USD

## Operational notes

- The digest is **read-only** against fleet state. It does not file issues,
  merge PRs, or spawn sessions. Safe to run alongside the cycle cron without
  any locking — the cycle cron's `director:cycle` lock doesn't apply here.
- Failures (Telegram 5xx, MCP unreachable, perceive() rate-limit) are logged
  to stdout but the cron still exits 0 unless the *send* itself failed — that
  way Luke gets a digest even when MCP is partially degraded, and Northflank
  marks the run failed only when Telegram won't accept the message.
