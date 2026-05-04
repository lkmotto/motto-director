# Director cadence

The director Northflank cron is split into two schedules so we can think
during business hours and ship while Luke sleeps.

## Schedules (UTC)

| Window | Cron expression | Local (CT) | Cadence |
|---|---|---|---|
| Overnight | `0,15,30,45 0-7 * * *` | 7pm – 2am CT | every 15 min |
| Daytime | `0 8-23 * * *` | 3am CT – 6pm CT | top of hour |

The overnight window is when long-running compound PRs flush through and
auto-merges land — high cadence is fine because no human is reviewing in
real time. The daytime window is hourly so the director's actions are
reviewable in near-real-time and don't churn the working tree.

## Applying the change

Northflank does not currently expose cron schedule edits via API in a
way we want to script against, so apply via the UI:

1. Open the Northflank `motto-director` cron job.
2. Duplicate it into two jobs (or create a second schedule on the same
   job, if available in your plan): `motto-director-night` and
   `motto-director-day`.
3. Set their cron expressions to the values above.
4. Disable the old single `*/30 * * * *` schedule.

`DIRECTOR_DRY_RUN=1` on either job is the panic switch — the cycle is
also gated by the `director:cycle` fleet lock (TTL 15 min) so two
schedules overlapping by accident is safe.

## Why these numbers

- 15-min overnight: matches the lock TTL; no two cycles can stomp.
- Hourly during the day: gives Luke a hard time-budget for reviewing the
  director's compound PR before the next tick widens it.
- 0-7 vs 8-23 split aligned to UTC hour boundaries so the two jobs never
  fire on the same minute. A `0 7 * * *` from the overnight window does
  not collide with `0 8 * * *` from the daytime window.
