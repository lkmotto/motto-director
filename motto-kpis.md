# Motto KPIs — north-star metrics the director steers toward

This file is read by the director on every cycle and prepended to the planner
lens's context. **Every epic the planner emits must explicitly tie back to
one or more of these KPIs**, and the `kpi_ref` field on each epic must match
a KPI title from this file exactly.

KPIs are organized by **repo** because each agent has a distinct job and
distinct success criteria. There is also a Tier-0 cluster of business-level
KPIs that no single repo owns — those are explicitly labeled "fleet-wide".

The DownTime project lives in `downtime-kpis.md`. It is a **separate product
line** from Motto Appraisal Service and is intentionally kept segregated
here so the director never proposes an epic that mixes the two.

> Edit current/target values here as you measure. The director uses the gap
> (target − current) to prioritize which epic to propose first.

---

## Tier 0 — Fleet-wide business outcomes

These directly drive the appraisal business's cash flow. No single repo
owns them; multiple agents combine to move them. The planner is allowed to
propose a fleet-wide epic that touches several repos, but each step must
land in exactly one repo.

### KPI: AMC panel registrations
- **target**: 42 high-priority Texas AMCs accepted (by 2026-08-01)
- **current**: 4 of 163 registered AMCs
- **contributing repos**: motto-sdr-agent (cold-warm sequence), motto-appraisal-site (credibility), motto-distribution (panel-targeted content)
- **leading indicator**: weekly AMC applications submitted ≥ 5
- **why it matters**: each panel is a recurring order pipeline; this is the single highest-leverage revenue input.

### KPI: New orders received per week
- **target**: 5 paid appraisal orders / week (sustained, by 2026-07-01)
- **current**: not measured (instrument first)
- **contributing repos**: motto-appraisal-pipeline (intake), motto-mcp-server (CRM tools), AMC panels (source)
- **leading indicator**: panels accepted × historical orders/panel/month
- **why it matters**: the only metric that directly maps to revenue.

### KPI: Discovery meetings booked per week
- **target**: ≥ 3 net-new discovery / intro calls booked from outbound (sustained, by 2026-07-01)
- **current**: not measured (sdr-agent emits sends + replies but no booked-meeting signal yet)
- **contributing repos**: motto-sdr-agent (cold email + Apollo sequences + Bland voice), motto-outreach (follow-ups), motto-mcp-server (cal.com booking webhook)
- **leading indicator**: positive replies / week × historical booking rate (~40% of positive replies)
- **why it matters**: meetings are the lead indicator for AMC panel registrations and new orders; this is the Apollo demand-gen north-star.
- **owning epic candidate**: `apollo-demand-gen` (Phase 4 of fleet centralization)

### KPI: Daily fleet LLM spend
- **target**: ≤ $5 / day blended across all agents
- **current**: ~$0.05–$0.15 / director cycle × 48 cycles/day plus SDR + social
- **contributing repos**: motto-fleet-burn-rate-tracker (measurement), every agent (consumption)
- **why it matters**: prevents one bug (e.g. stuck loop) from torching runway.

### KPI: Northflank monthly hosting cost
- **target**: ≤ $50 / month total fleet hosting
- **current**: not measured (need NF billing API integration)
- **contributing repos**: motto-fleet-burn-rate-tracker
- **why it matters**: same — runway is the primary constraint.

---

## Per-repo KPIs (Motto Appraisal Service fleet)

## Repo: motto-director
*Self-aware orchestrator: perceives the fleet, ideates moves, gates approvals.*

### KPI: Director moves applied per week
- **target**: ≥ 30 approved+applied moves / week (sustained autonomous output)
- **current**: ~1 (Luke rejected id=1; rest in pending awaiting review)
- **leading indicator**: pending → approved conversion rate ≥ 60%
- **why it matters**: this is "labor utilization." If approvals don't flow, the fleet is idle.

### KPI: Active epics in flight
- **target**: 3 active multi-cycle epics, each closing within ≤ 5 cycles after open
- **current**: 0 (this file + the planner ship in PR #58)
- **leading indicator**: epic close rate, average cycles-to-close
- **why it matters**: forces the director to think in projects, not chores.

### KPI: Median PR time-to-merge across motto-* repos
- **target**: < 24 hours for any PR with green CI + 1 review
- **current**: several PRs > 285 hours stale (#1, #3, #4, #5)
- **owner-lens**: stale_pr_closer + auto-merge gate when label present
- **why it matters**: stale PRs are unrealized value rotting on the vine.

### KPI: CI green rate across motto-* repos
- **target**: ≥ 95% on `main`, last 10 runs per repo
- **current**: not measured
- **owner-lens**: ci_doctor
- **why it matters**: red CI blocks every other agent from improving itself.

---

## Repo: motto-sdr-agent
*Autonomous outbound: Apollo autopilot, Lavender-scored cold email, voice follow-up.*

### KPI: Outbound email sends per day (warmed mailboxes)
- **target**: 100–200 sends / day, sustained
- **current**: capacity exists but not consistently hitting target
- **leading indicator**: per-mailbox deliverability score, daily quota utilization
- **why it matters**: top of the AMC + lender funnel.

### KPI: Cold email reply rate
- **target**: ≥ 5% reply, ≥ 1% positive reply
- **current**: not instrumented
- **leading indicator**: subject-line A/B winners, persona match score
- **why it matters**: volume without replies is wasted spend.

### KPI: AMC applications submitted per week
- **target**: ≥ 5 / week
- **current**: ~0–1 (manual)
- **leading indicator**: prospects researched, applications ready in queue
- **why it matters**: feeds Tier-0 AMC panel KPI directly.

### KPI: Apollo CRM data freshness
- **target**: ≥ 95% of contacts have last_enriched_at within 30 days
- **current**: not measured
- **why it matters**: stale enrichment → wrong personalization → worse replies.

---

## Repo: motto-appraisal-pipeline
*Order intake → comp hunt → report production → delivery.*

### KPI: Orders processed end-to-end without human touch
- **target**: ≥ 50% of orders auto-progress past comp-selection without manual gate
- **current**: 0% (pipeline is HTTP `/tick` driven; manual gates dominate)
- **leading indicator**: gate-pass rate at each stage (intake, comps, value reconciliation)
- **why it matters**: this is the labor savings the whole stack exists for.

### KPI: Median order intake → first comp set time
- **target**: ≤ 30 minutes
- **current**: not measured
- **why it matters**: time-to-first-comp is the user-perceptible speed of the service.

### KPI: Median appraisal report turn time (intake → client delivery)
- **target**: ≤ 3 business days end-to-end (sustained, by 2026-09-01)
- **current**: not measured (need stage-level timestamps in the pipeline)
- **contributing repos**: motto-appraisal-pipeline (every stage), comp-hunter (comp selection), motto-mcp-server (timer instrumentation), motto-appraisal-cockpit (human-gate latency)
- **leading indicator**: median dwell time at slowest gate; appraiser-pending queue depth
- **why it matters**: shorter turn times = more orders fit in the same calendar = revenue ceiling rises without hiring. This is the Phase-4 "appraisal-turn-time" target.
- **owning epic candidate**: `appraisal-turn-time` (Phase 4 of fleet centralization)

### KPI: Pipeline tick failure rate
- **target**: ≤ 2% of `/tick` calls error
- **current**: not measured (need NF + Langfuse cross-join)
- **owner-lens**: cost_watchdog + ci_doctor
- **why it matters**: a flaky pipeline blocks every order.

---

## Repo: motto-mcp-server
*FastMCP host: every agent's tool surface (Sheets, CRM, lead gen, fleet events).*

### KPI: MCP tool contract test coverage
- **target**: 100% of public tools have a contract test asserting input/output schema
- **current**: not instrumented
- **why it matters**: agents calling agents must not silently break.

### KPI: MCP server uptime (HTTP /health)
- **target**: ≥ 99.5% measured by external prober
- **current**: not measured
- **why it matters**: it's a single point of failure for the fleet.

### KPI: CLAUDE.md present and ≤ 30 days old
- **target**: yes (currently flagged missing by deep-read)
- **current**: missing
- **owner-lens**: opportunity_scout
- **why it matters**: spawned Claude Code sessions read CLAUDE.md first.

---

## Repo: motto-social-agent
*Autonomous LinkedIn / Instagram / Facebook posting.*

### KPI: Posts shipped per week
- **target**: ≥ 5 posts / week across the 3 platforms (sustained)
- **current**: not measured
- **why it matters**: brand impressions feed Tier-0 panel acceptance probability.

### KPI: Engagement rate (likes+comments / impressions)
- **target**: ≥ 2% on LinkedIn (industry baseline ~1.5%)
- **current**: not instrumented
- **leading indicator**: hook variant performance, post-time A/B
- **why it matters**: low-engagement posts waste posting credits.

---

## Repo: motto-video-agent
*Long-form YouTube assembly: Kling 2.6 + ElevenLabs + FFmpeg.*

### KPI: Videos published per month
- **target**: ≥ 4 / month (one per week)
- **current**: not measured
- **why it matters**: YouTube watch-time compounds; this is the slowest-but-stickiest channel.

### KPI: Average video production cost
- **target**: ≤ $3 in API spend per finished video
- **current**: not measured (Kling + ElevenLabs costs not aggregated)
- **why it matters**: the only video pipeline that can be run weekly without bleeding cash.

---

## Repo: motto-shortform
*FORGE — programmatic short-form video pipeline (Reels, TikTok, Shorts).*

### KPI: Shorts shipped per week across platforms
- **target**: ≥ 7 / week (one per day cadence across 3 platforms)
- **current**: not measured
- **why it matters**: short-form is the highest-velocity discovery channel.

### KPI: Hook performance scoring instrumented
- **target**: 100% of shorts get an A/B-able hook variant + 24h retention measurement
- **current**: not instrumented
- **why it matters**: without measurement, we can't iterate on what wins.

---

## Repo: motto-distribution
*Multi-platform content fanout: LinkedIn + X + Reddit + Beehiiv + Facebook Groups.*

### KPI: Distribution coverage per piece of content
- **target**: ≥ 5 platforms touched per piece automatically
- **current**: not measured
- **why it matters**: amortizes content production cost across reach.

### KPI: Posting failure rate
- **target**: ≤ 5% of scheduled posts fail
- **current**: not measured
- **owner-lens**: ci_doctor + cost_watchdog
- **why it matters**: silent failures = silent disappearance from feeds.

---

## Repo: motto-fleet-burn-rate-tracker
*Daily fleet cost aggregation (NF + DO + Cloudflare + LLM providers).*

### KPI: Cost data freshness
- **target**: every agent has a daily cost row, < 24h old
- **current**: partial (LLM via Langfuse; NF/DO not yet wired)
- **why it matters**: blind to spend → no defense against runaway costs.

### KPI: Anomaly detection coverage
- **target**: alert when any agent's 24h spend > 2× its 7-day median
- **current**: not implemented
- **why it matters**: a stuck loop should page Telegram in minutes, not days.

---

## Repo: motto-finance-tracker
*Plaid sync + bank reconciliation + invoice tracking for the appraisal business.*

### KPI: Plaid transactions synced lag
- **target**: < 24h between bank settlement and tracker row
- **current**: not measured
- **why it matters**: cash-flow visibility powers every "should we scale ads?" decision.

---

## Repo: motto-appraisal-cockpit
*Human-in-the-loop web cockpit: order submission, automation-gate timeline, director approvals.*

### KPI: Director pending → approved median latency
- **target**: ≤ 4 hours during waking hours (10am–3am CT)
- **current**: 285+ hours on initial backlog (Luke catching up)
- **why it matters**: the cockpit is the fleet's bottleneck. Slow approval = idle agents.

### KPI: Cockpit views with active human-task list
- **target**: 100% of pending agent decisions have a one-click cockpit action
- **current**: ~80% (director pending list yes; per-agent gates partial)
- **why it matters**: any approval that requires a CLI is a paper jam.

---

## Repo: motto-credential-grabber
*Ephemeral credential rotator: rotates fleet API keys, writes to Doppler.*

### KPI: Stale credentials in Doppler
- **target**: 0 secrets in `motto-core/prd` older than rotation policy (90 days for most)
- **current**: not measured
- **why it matters**: rotated keys = blast-radius reduction.

---

## Repo: motto-outreach
*Reddit + X outreach for Motto.*

### KPI: Outreach replies opened by humans
- **target**: ≥ 10 / week qualified handoffs to Luke
- **current**: not measured
- **why it matters**: organic outreach is unpaid TOFU.

---

## Repo: motto-linkedin-ads
*LinkedIn paid: engagement scoring, TLA briefs, CAC tracking.*

### KPI: Blended CAC for AMC + lender leads
- **target**: ≤ $200 per qualified AMC contact
- **current**: not measured
- **why it matters**: CAC must be < LTV to scale paid spend.

---

## Repo: rw-order-monitor
*Renters Warehouse Gmail watcher (recurring AMC).*

### KPI: RW orders captured / actual RW orders sent
- **target**: ≥ 99% (zero misses)
- **current**: not instrumented (no ground truth feed)
- **why it matters**: a missed order = lost revenue + reputation hit with RW.

---

## Repo: appraisalos-bidding
*AppraisalOS auto-bid daemon (DigitalOcean).*

### KPI: Bids placed within order TTL
- **target**: ≥ 95% of eligible orders get a bid before expiry
- **current**: not measured
- **why it matters**: a missed bid is a missed order.

---

## How the planner uses this file

When the planner runs (manual fire only, heavy deep-read only), it:
1. Reads this file + `motto-strategy.md` + the deep-read repo evidence
2. Identifies the top-3 KPIs with the largest gap between target and current
3. Emits 1–3 multi-step **epics** — each is JSON with title, kpi_ref (must match a KPI heading above), estimated_cycles, and an ordered plan of 3–8 concrete moves
4. Stores them in the `epics` Neon table with status='proposed'
5. Once approved (cockpit), `epic_executor` picks the next concrete move per cycle and queues it as a normal pending_move

The director will not propose an epic for a KPI that already has an open epic — one project per KPI at a time.

DownTime KPIs live in `downtime-kpis.md` and are loaded only when the planner targets the DownTime repos.
