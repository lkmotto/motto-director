"""Phase 5.9: weekly quality flywheel cron.

Sibling to ``director.meta``. Where ``meta`` asks an LLM for advisory
PRs, the flywheel uses *deterministic* signal-driven heuristics across
three sources:

    * Langfuse traces (per-tool error rate, p95 latency)
    * Neon control plane (task success, repeated failures, overturns)
    * GitHub PR outcomes across the fleet (merge rate, reverts, CI flake)

It then turns high-confidence cheap fixes into PRs (config / prompt
overrides only — never protected modules), expensive or protected fixes
into tracking issues, persists a ``QualityReport`` to
``quality_reports``, and posts a Telegram digest.

No-ops gracefully when any of the three signal sources are unconfigured —
each source returns an empty blob the synthesizer treats as a degraded
signal, not an error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

from director.observability import event, init_observability, register, track_run
from director.quality import github_signals, langfuse_signals, postgres_signals, pr_generator
from director.quality.synthesizer import synthesize

logger = logging.getLogger(__name__)


def _log(event_name: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event_name,
        **fields,
    }
    print(json.dumps(record, default=str), flush=True)


async def _run_async() -> int:
    await register()

    async with track_run("quality.flywheel", intent="weekly_quality_flywheel") as run:
        _log("quality.start")
        await event("quality.start", level="info")

        # Collect three signals in parallel where possible. github_signals
        # is sync (subprocess), so wrap in to_thread.
        lf_task = asyncio.create_task(langfuse_signals.collect())
        pg_task = asyncio.create_task(postgres_signals.collect())
        gh_task = asyncio.create_task(asyncio.to_thread(github_signals.collect))

        lf, pg, gh = await asyncio.gather(lf_task, pg_task, gh_task)

        _log(
            "quality.signals",
            langfuse_configured=lf.get("configured"),
            postgres_configured=pg.get("configured"),
            github_configured=gh.get("configured"),
            trace_count=lf.get("trace_count", 0),
        )

        report = synthesize(lf, pg, gh)
        report_dict = report.as_dict()

        _log(
            "quality.synthesized",
            top_problems=report.top_problems,
            confidence=report.confidence_score,
            fix_count=len(report.suggested_fixes),
        )

        # PR / issue generation is sync (gh subprocess) — wrap.
        gen = await asyncio.to_thread(pr_generator.generate, report)
        report_dict["pr_urls"] = gen.pr_urls
        report_dict["issue_urls"] = gen.issue_urls

        _log(
            "quality.generated",
            pr_count=len(gen.pr_urls),
            issue_count=len(gen.issue_urls),
            skipped=len(gen.skipped),
        )

        # Persist (best-effort; ignored when Neon unconfigured / migration unrun).
        row_id: int | None = None
        try:
            row_id = await postgres_signals.persist_quality_report(report_dict)
        except Exception as exc:  # noqa: BLE001
            logger.warning("persist_quality_report raised: %s", exc)

        # Post a Telegram digest of what happened. Best-effort.
        try:
            from director.digest import send_telegram

            text = _format_digest(
                report_dict, gen.pr_urls, gen.issue_urls, len(gen.skipped)
            )
            await asyncio.to_thread(send_telegram, text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("telegram digest skipped: %s", exc)

        await event(
            "quality.done",
            level="info",
            confidence=report.confidence_score,
            pr_count=len(gen.pr_urls),
            issue_count=len(gen.issue_urls),
            row_id=row_id,
        )
        run.summary["confidence"] = report.confidence_score
        run.summary["pr_count"] = len(gen.pr_urls)
        run.summary["issue_count"] = len(gen.issue_urls)
        run.summary["row_id"] = row_id or 0

        _log(
            "quality.done",
            pr_urls=gen.pr_urls,
            issue_urls=gen.issue_urls,
            row_id=row_id,
        )
    return 0


def _format_digest(
    report_dict: dict[str, Any],
    pr_urls: list[str],
    issue_urls: list[str],
    skipped: int,
) -> str:
    """Markdown-flavored digest. Telegram supports HTML via parse_mode but
    digest.send_telegram uses plain text — keep it readable either way."""
    lines: list[str] = []
    lines.append("*director quality flywheel*")
    lines.append(f"confidence: {report_dict.get('confidence_score', 0.0):.2f}")
    top = report_dict.get("top_problems") or []
    if top:
        lines.append("top problems:")
        for p in top[:3]:
            lines.append(f"  - {p}")
    if pr_urls:
        lines.append("PRs opened:")
        for u in pr_urls:
            lines.append(f"  {u}")
    if issue_urls:
        lines.append("issues opened:")
        for u in issue_urls:
            lines.append(f"  {u}")
    if skipped:
        lines.append(f"skipped: {skipped}")
    if not pr_urls and not issue_urls:
        lines.append("no actionable fixes this run")
    return "\n".join(lines)


def run_quality() -> int:
    """Sync entry point for the `motto-director-quality` console script."""
    init_observability("motto-director")
    if os.environ.get("DIRECTOR_QUALITY_FLYWHEEL_ENABLED", "1") != "1":
        _log("quality.disabled")
        return 0
    return asyncio.run(_run_async())


if __name__ == "__main__":
    sys.exit(run_quality())
