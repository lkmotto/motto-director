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

### Repo: downtime-event-agent
Crawls public event feeds (Eventbrite, city calendars, venue sites) and
writes normalized events into the DownTime Postgres.

- **events ingested per week**: target ≥ 200 (current TBD)
- **dedup rate**: target ≥ 95 % unique after canonicalization
  (title+venue+start_time hash)
- **ingestion run failure rate**: target < 5 % of scheduled runs error
- **CLAUDE.md present + up to date**: target = yes
- **why these**: ingestion is the top of the funnel. If it's flaky or
  duplicative, the email digest looks empty or repetitive.

### Repo: downtime-email-agent
Renders the Friday 8am DFW weekend digest from Postgres and ships via
Mailgun/Resend.

- **send success rate**: target ≥ 99 % of subscribers receive Friday email
- **render time**: target < 30 s end-to-end (query → HTML → send)
- **bounce rate**: target ≤ 2 % (deliverability hygiene)
- **unsubscribe rate per send**: target ≤ 0.5 %
- **why these**: this is the customer-visible artifact. Anything broken
  here destroys subscriber trust faster than ingestion gaps do.

### Repo: downtime-backend
The Postgres + API layer that powers both ingestion writes and email reads.

- **API uptime**: target 99.9 % monthly
- **median read latency**: target < 100 ms for /events queries
- **schema migration safety**: target = zero broken deploys (every
  migration is reversible + tested in staging first)
- **why these**: backend stability lets the other two agents run without
  retry storms. Latency matters for the iOS app reading live events.

### Repo: downtime-app
The consumer-facing iOS/web app that reads `downtime-backend`.

- **DAU / WAU ratio**: target ≥ 0.4 (sticky weekly use, not ghost installs)
- **median session length**: target ≥ 90 s (long enough to actually plan)
- **crash-free sessions**: target ≥ 99.5 %
- **why these**: app stickiness is the main differentiator vs the email-
  only experience. If users only open it once and never return, the app
  isn't earning its build cost.

### Repo: downtime-dfw
The DFW-specific configuration / venue corpus / categorization rules. Acts
as the "which sources to crawl + which neighborhood does each venue belong
to" knowledge base.

- **venue catalog coverage**: target ≥ 500 active DFW venues mapped to
  neighborhoods + categories
- **categorization accuracy**: target ≥ 90 % of events tagged correctly on
  first pass (sample audit weekly)
- **stale source detection**: target ≤ 7 days lag from "source goes 404"
  to "source removed from crawler"
- **why these**: the categorization quality is what makes the email feel
  like a curated friend's recommendation vs an undifferentiated firehose.

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
