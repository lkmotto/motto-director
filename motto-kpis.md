# Motto KPIs — north-star metrics the director steers toward

This file is read by the director on every cycle and prepended to the planner
lens's context. Every epic the planner emits must explicitly tie back to one
or more of these KPIs.

Edit the targets and current values here. The director uses this file (not
chat) as the source of truth. Update the `current` value when you measure;
the planner uses the gap between current and target to prioritize epics.

---

## Tier 0 — Survival metrics (revenue first)

These directly drive the appraisal business's cash flow. The fleet exists
to move these numbers.

### KPI: AMC panel registrations
- **target**: 42 high-priority Texas AMCs accepted (by 2026-08-01)
- **current**: 4 of 163 registered AMCs
- **owner**: motto-sdr-agent (cold-warm sequence) + manual applications
- **leading indicator**: weekly applications submitted ≥ 5
- **why it matters**: each panel is a recurring order pipeline; this is the
  single highest-leverage revenue input.

### KPI: New orders received per week
- **target**: 5 paid appraisal orders / week (sustained, by 2026-07-01)
- **current**: not measured (instrument first)
- **owner**: motto-appraisal-pipeline (intake) + AMC panels (source)
- **leading indicator**: panels accepted × historical orders/panel/month
- **why it matters**: the only metric that directly maps to revenue.

### KPI: Outbound email volume + reply rate
- **target**: 100–200 sends / day across warmed mailboxes, ≥ 5% reply rate,
  ≥ 1% positive-reply rate
- **current**: capacity exists for 50–200/day; reply rate not instrumented
- **owner**: motto-sdr-agent
- **leading indicator**: sends/day, deliverability score per mailbox
- **why it matters**: top of the AMC + lender funnel.

---

## Tier 1 — Velocity & throughput

How fast the fleet learns and ships.

### KPI: Director moves applied per week
- **target**: ≥ 30 approved+applied moves / week (sustained autonomous output)
- **current**: ~1 (Luke rejected id=1; rest of light/medium runs are in
  pending awaiting review)
- **owner**: motto-director
- **leading indicator**: pending → approved conversion rate ≥ 60%
- **why it matters**: this is "labor utilization" — the whole reason the
  director exists. If approvals don't flow, the fleet is idle.

### KPI: Active epics in flight
- **target**: 3 active multi-cycle epics, each closing ≤ 5 cycles after open
- **current**: 0 (this file + the planner ship in this PR)
- **owner**: motto-director (planner + epic_executor)
- **leading indicator**: epic close rate, average cycles-to-close
- **why it matters**: forces the director to think in projects, not chores.

### KPI: Median PR time-to-merge (motto repos)
- **target**: < 24 hours for any PR with green CI + 1 review
- **current**: several PRs > 285 hours stale (#1, #3, #4, #5)
- **owner**: motto-director (auto-merge gate when label present)
- **leading indicator**: count of CI-green PRs older than 24h
- **why it matters**: stale PRs are unrealized value rotting on the vine.

---

## Tier 2 — Cost & reliability

Don't blow the runway.

### KPI: Daily fleet LLM spend
- **target**: ≤ $5 / day blended across all agents
- **current**: ~$0.05–0.15 / director cycle × 48 cycles/day = ~$2.40-$7.20
  if cron unpaused. SDR + social adds more.
- **owner**: motto-fleet-burn-rate-tracker
- **leading indicator**: per-agent Langfuse cost tag
- **why it matters**: prevents one bug (e.g. stuck loop) from torching budget.

### KPI: Northflank monthly hosting cost
- **target**: ≤ $50 / month total fleet hosting
- **current**: not measured (need NF billing API integration)
- **owner**: motto-fleet-burn-rate-tracker
- **why it matters**: same — the runway is the primary constraint.

### KPI: CI green rate across motto-* repos
- **target**: ≥ 95% on main branch, last 10 runs
- **current**: not measured
- **owner**: motto-director (ci_doctor lens)
- **why it matters**: red CI blocks every other agent from improving itself.

---

## Tier 3 — Knowledge & evals

Self-improvement infrastructure. Don't let the fleet calcify.

### KPI: Repos with up-to-date CLAUDE.md
- **target**: 100% of WATCH_REPOS have a CLAUDE.md updated within 30 days
- **current**: motto-mcp-server is missing one (heavy deep-read flagged it)
- **owner**: motto-director (opportunity_scout lens)
- **why it matters**: the CLAUDE.md is what spawned Claude Code sessions
  read first; out-of-date or missing → wasted sessions.

### KPI: Eval coverage on critical paths
- **target**: every public motto-mcp-server tool has a contract test
- **current**: not instrumented
- **owner**: motto-director (opportunity_scout)
- **why it matters**: agents calling agents must not silently break.

---

## How the planner uses this

When the planner runs (manual fire only for now), it:
1. Reads this file + motto-strategy.md + the deep-read repo evidence
2. Identifies the top 3 KPIs with the largest gap between target and current
3. Emits 1–3 multi-step *epics* — each is a JSON object with title, kpi_ref,
   estimated cycles, and an ordered plan of 3–8 concrete moves
4. Stores them in the `epics` Neon table with status='proposed'
5. Once approved (cockpit), epic_executor picks the next concrete move from
   the open epic on each cycle and queues it as a normal pending_move

The director will not propose an epic for a KPI that already has an open
epic — one project per KPI at a time.
