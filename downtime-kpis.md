# DownTime KPIs — separate product line from Motto Appraisal Service

This file is read by the director **only when an epic is explicitly scoped
to a DownTime repo**. It is intentionally segregated from `motto-kpis.md`
so the planner never proposes a cross-product epic that mixes DownTime
event/email automation with the Motto Appraisal Service fleet.

DownTime is a DFW-area "things to do this weekend" event aggregator with
its own backend, ingestion agents, and weekly email digest. It shares zero
business KPIs with the appraisal service.

> Edit current/target values here as you measure. The planner uses the gap
> (target − current) to prioritize which DownTime epic to propose first.
> Every KPI gets its own `### KPI: <title>` heading — that's the exact
> string the planner must write into `kpi_ref`.

---

## Tier 0 — DownTime product outcomes

These directly drive the DownTime newsletter business. No single repo owns
them; the ingestion + email + backend agents combine to move them.

### KPI: Weekly digest open rate
- **target**: ≥ 35 % open rate on Friday 8am send (industry good = 25 %)
- **current**: not measured yet (post-Mailgun integration)
- **owners**: `downtime-email-agent`, `downtime-backend`
- **why**: open rate is the cleanest signal of whether the digest content
  matches what the audience actually wants to do this weekend. Below 25 %
  the product has no audience-market fit.

### KPI: Weekly active subscribers
- **target**: 1,000 active subscribers by 2026-09-01
- **current**: TBD (waitlist + early signups)
- **owners**: `downtime-app`, `downtime-backend`
- **why**: subscriber growth is the leading indicator of monetization
  potential (sponsorship, ticket affiliate, premium tier).

### KPI: Events covered per Friday digest
- **target**: ≥ 50 unique DFW events per weekly send (Fri-Sun window)
- **current**: depends on ingestion freshness
- **owners**: `downtime-event-agent`, `downtime-dfw`
- **why**: subscribers churn if the digest feels thin or repetitive. 50
  events lets the email agent cluster by neighborhood/category and still
  surface variety.

---

## Per-repo KPIs (DownTime fleet)

## Repo: downtime-event-agent
Crawls public event feeds (Eventbrite, city calendars, venue sites) and
writes normalized events into the DownTime Postgres.

### KPI: Events ingested per week (downtime-event-agent)
- **target**: ≥ 200 / week
- **current**: TBD
- **why**: ingestion is the top of the funnel. Thin ingestion = thin digest.

### KPI: Event dedup rate (downtime-event-agent)
- **target**: ≥ 95 % unique after canonicalization (title+venue+start_time hash)
- **current**: TBD
- **why**: duplicates make the digest look lazy and repetitive.

### KPI: Ingestion run failure rate (downtime-event-agent)
- **target**: < 5 % of scheduled runs error
- **current**: TBD
- **why**: a flaky ingestor compounds: missed events on Tuesday show up as
  a hollow digest on Friday.

## Repo: downtime-email-agent
Renders the Friday 8am DFW weekend digest from Postgres and ships via
Mailgun/Resend.

### KPI: Friday digest send success rate (downtime-email-agent)
- **target**: ≥ 99 % of subscribers receive the Friday email
- **current**: TBD
- **why**: this is the customer-visible artifact; missed sends destroy
  subscriber trust faster than ingestion gaps do.

### KPI: Email render end-to-end time (downtime-email-agent)
- **target**: < 30 s from query → HTML → send dispatch
- **current**: TBD
- **why**: long render times stack up under load; we want headroom.

### KPI: Email bounce rate (downtime-email-agent)
- **target**: ≤ 2 % (Mailgun deliverability hygiene)
- **current**: TBD
- **why**: high bounces poison sender reputation across the whole list.

### KPI: Unsubscribe rate per send (downtime-email-agent)
- **target**: ≤ 0.5 %
- **current**: TBD
- **why**: spike here = digest content drifted from what subscribers signed
  up for; investigate before scaling list growth.

## Repo: downtime-backend
The Postgres + API layer that powers both ingestion writes and email reads.

### KPI: API uptime (downtime-backend)
- **target**: 99.9 % monthly
- **current**: TBD
- **why**: backend stability lets the other two agents run without retry storms.

### KPI: Median /events read latency (downtime-backend)
- **target**: < 100 ms
- **current**: TBD
- **why**: slow reads hurt the iOS app's perceived snappiness.

### KPI: Schema migration safety (downtime-backend)
- **target**: zero broken deploys (every migration reversible + staging-tested)
- **current**: TBD
- **why**: a bad migration takes the whole product offline.

## Repo: downtime-app
The consumer-facing iOS/web app that reads `downtime-backend`.

### KPI: DAU / WAU ratio (downtime-app)
- **target**: ≥ 0.4 (sticky weekly use, not ghost installs)
- **current**: TBD
- **why**: stickiness is the differentiator vs the email-only experience.

### KPI: Median session length (downtime-app)
- **target**: ≥ 90 s (long enough to actually plan)
- **current**: TBD
- **why**: short sessions mean the app isn't earning its build cost.

### KPI: Crash-free session rate (downtime-app)
- **target**: ≥ 99.5 %
- **current**: TBD
- **why**: crashes are the fastest path to uninstall.

## Repo: downtime-dfw
The DFW-specific configuration / venue corpus / categorization rules.

### KPI: Venue catalog coverage (downtime-dfw)
- **target**: ≥ 500 active DFW venues mapped to neighborhoods + categories
- **current**: TBD
- **why**: catalog depth lets the email cluster by neighborhood/category
  with variety instead of repeating the same venues.

### KPI: Event categorization accuracy (downtime-dfw)
- **target**: ≥ 90 % of events tagged correctly on first pass (weekly audit)
- **current**: TBD
- **why**: correct tags are what make the digest feel curated rather than
  an undifferentiated firehose.

### KPI: Stale source detection lag (downtime-dfw)
- **target**: ≤ 7 days from "source goes 404" to "source removed from crawler"
- **current**: TBD
- **why**: dead sources eat ingestion runtime and produce zero events.

---

## How the planner uses this file

When a perceive snapshot points at a DownTime repo OR a DownTime epic is
already active, the planner is given **only this file** as KPI context
(not motto-kpis.md). When perceive is scoped to Motto repos, the planner
is given **only motto-kpis.md**.

This guarantees:
1. The planner never writes a `kpi_ref` that mixes DownTime and Motto
2. The planner never proposes a step in a Motto repo that "improves"
   DownTime KPIs (or vice versa)
3. Tier-0 lists stay clean per product line — DownTime business outcomes
   live here; appraisal-service business outcomes live in motto-kpis.md

If you ever want to cross-pollinate (e.g. let the SDR agent send DownTime
sponsorship outreach), do it as a deliberate epic that explicitly lives
in **one** product line, not by merging KPI files.
