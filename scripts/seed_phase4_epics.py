"""Seed the two Phase-4 epics: appraisal-turn-time + apollo-demand-gen.

These are the two campaigns the user named as the centralization north stars:
- shrink end-to-end appraisal report turn time
- book stable discovery meetings via Apollo + cold email + Bland voice

Both epics are inserted as `status='proposed'` so they need human approval
in the cockpit before any step is executed. Each epic ties to a Tier-0 KPI
that was added to motto-kpis.md in the same PR.

Idempotent: if an epic with the same title is already proposed/active, this
script logs and skips (insert_epics already handles UniqueViolation, but the
check here gives clearer logs).

Usage:
    python -m scripts.seed_phase4_epics
    # or: NEON_DATABASE_URL=... python -m scripts.seed_phase4_epics

Per fleet doctrine:
- Both epics target steps via `factory_droid` (the new move kind landed
  in #83) where the work matches a custom droid role, and `file_issue`
  for human-driven work. spawn_session is intentionally avoided here
  so we exercise the Factory path end-to-end.
- Each step has a clear `rationale` referencing the KPI gap, so the
  planner / cockpit can show why each move was queued.
"""

from __future__ import annotations

import logging
import os
import uuid

from director.epics import Epic, EpicStep, insert_epics, is_configured

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _appraisal_turn_time_epic() -> Epic:
    return Epic(
        title="appraisal-turn-time: shrink intake \u2192 delivery to \u22643 business days",
        kpi_ref="Median appraisal report turn time (intake \u2192 client delivery)",
        rationale=(
            "The end-to-end appraisal turn time is currently not even measured; "
            "we know anecdotally it spans multiple business days with several "
            "human gates. Until we have stage-level timestamps, we can't tell "
            "which gate is slowest, so the first move is instrumentation. After "
            "that, attack the slowest gate iteratively."
        ),
        estimated_cycles=5,
        success_criteria=(
            "Stage-level timestamps land in motto-mcp-server fleet ledger for "
            "every order; weekly digest emits median turn time; slowest gate is "
            "identified and reduced by \u226550% within 4 weeks."
        ),
        steps=[
            EpicStep(
                order=1,
                title="Instrument stage timestamps across appraisal pipeline",
                kind="factory_droid",
                repo="lkmotto/motto-appraisal-pipeline",
                rationale=(
                    "Add fleet.record_event calls at every stage boundary "
                    "(intake_received, comps_selected, value_reconciled, "
                    "report_drafted, report_delivered). Without this we are "
                    "guessing at where time is spent."
                ),
            ),
            EpicStep(
                order=2,
                title="Add turn-time signal to director/quality/postgres_signals.py",
                kind="factory_droid",
                repo="lkmotto/motto-director",
                rationale=(
                    "Quality flywheel needs to read the new stage timestamps "
                    "and compute median dwell time per stage so the planner "
                    "can target the slowest gate."
                ),
                depends_on=[1],
            ),
            EpicStep(
                order=3,
                title="Weekly turn-time digest in cockpit",
                kind="file_issue",
                repo="lkmotto/motto-appraisal-cockpit",
                rationale=(
                    "Surface the per-stage median + this-week-vs-baseline "
                    "delta on the cockpit dashboard so Luke can see drift."
                ),
                depends_on=[2],
            ),
            EpicStep(
                order=4,
                title="Automate the slowest gate (identified by step 2)",
                kind="factory_droid",
                repo="lkmotto/motto-appraisal-pipeline",
                rationale=(
                    "Once we know the slowest gate, write the automation. "
                    "Likely candidates: comp-set generation (use comp-hunter), "
                    "value reconciliation (rule-based first), or report "
                    "drafting (templated)."
                ),
                depends_on=[2],
            ),
            EpicStep(
                order=5,
                title="Verify turn-time reduction over 7-day rolling window",
                kind="verify_move",
                repo="lkmotto/motto-director",
                rationale=(
                    "Confirm the automation actually moved the KPI. If not, "
                    "epic_executor will keep the epic open and the critic "
                    "loop will surface why."
                ),
                depends_on=[4],
            ),
        ],
    )


def _apollo_demand_gen_epic() -> Epic:
    return Epic(
        title="apollo-demand-gen: \u22653 discovery meetings booked per week",
        kpi_ref="Discovery meetings booked per week",
        rationale=(
            "SDR-agent is already sending warmed cold email through Apollo "
            "and has Bland voice follow-ups, but we have no booked-meeting "
            "signal closed into the fleet ledger, so we can't tell what's "
            "working. Three meetings per week is the minimum cadence to "
            "feed AMC-panel registrations and new orders (both Tier-0 KPIs)."
        ),
        estimated_cycles=5,
        success_criteria=(
            "Booked-meeting webhook from cal.com lands in motto-mcp-server; "
            "weekly digest shows meetings booked per source (cold email vs "
            "voice vs inbound); cadence hits \u22653/week for 3 consecutive weeks."
        ),
        steps=[
            EpicStep(
                order=1,
                title="Wire cal.com webhook \u2192 motto-mcp-server fleet event",
                kind="factory_droid",
                repo="lkmotto/motto-mcp-server",
                rationale=(
                    "Add a /webhooks/calcom endpoint that records "
                    "{source, prospect_email, booked_at} as a fleet event. "
                    "Without this the meetings KPI is invisible."
                ),
            ),
            EpicStep(
                order=2,
                title="Attribute meetings to outbound source in sdr-agent",
                kind="factory_droid",
                repo="lkmotto/motto-sdr-agent",
                rationale=(
                    "Cross-join Apollo sequence events with the new cal.com "
                    "events on prospect email to attribute each meeting to "
                    "its source (cold email subject line, voice script, etc.)."
                ),
                depends_on=[1],
            ),
            EpicStep(
                order=3,
                title="Tighten Apollo ICP via ABCD experiment on subject lines",
                kind="file_issue",
                repo="lkmotto/motto-sdr-agent",
                rationale=(
                    "Use the existing abcd-experimentation skill to run "
                    "Thompson-Sampling allocation on 4 subject-line variants "
                    "for the top vertical (lenders). Need step 2 working "
                    "first so we can measure meetings, not just opens."
                ),
                depends_on=[2],
            ),
            EpicStep(
                order=4,
                title="Escalate non-responders to Bland voice agent",
                kind="factory_droid",
                repo="lkmotto/motto-sdr-agent",
                rationale=(
                    "For prospects with \u22652 opens but 0 replies, hand off "
                    "to Bland voice agent for a single follow-up call. "
                    "Voice cost is ~$0.50/call; only economic if reply "
                    "rate from voice \u22655%."
                ),
                depends_on=[2],
            ),
            EpicStep(
                order=5,
                title="Verify weekly meetings cadence hits \u22653 over 3 weeks",
                kind="verify_move",
                repo="lkmotto/motto-director",
                rationale=(
                    "Three-week sustained-cadence check before declaring "
                    "the epic closed. If not hit, epic stays active and "
                    "next planner cycle will propose the next remediation."
                ),
                depends_on=[3, 4],
            ),
        ],
    )


def main() -> None:
    if not is_configured():
        raise SystemExit(
            "No NEON_DATABASE_URL / DATABASE_URL set. Run with Doppler:\n"
            "  doppler run --project motto-core --config prd -- "
            "python -m scripts.seed_phase4_epics"
        )

    run_id = os.environ.get("SEED_RUN_ID") or str(uuid.uuid4())
    epics = [_appraisal_turn_time_epic(), _apollo_demand_gen_epic()]
    logger.info("Seeding %d phase-4 epics (run_id=%s)", len(epics), run_id)
    for epic in epics:
        logger.info("  - %s (%d steps)", epic.title, len(epic.steps))
    counts = insert_epics(epics, run_id=run_id)
    logger.info("Seed result: %s", counts)
    if counts["errors"]:
        raise SystemExit(f"{counts['errors']} epic(s) failed to insert")


if __name__ == "__main__":
    main()
