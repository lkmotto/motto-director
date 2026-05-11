"""Synthesizer: turn three signal blobs into a QualityReport.

Deterministic, no-LLM. Rule-based detection of common failure patterns
the director should self-heal:

    * High-error-rate tools (Langfuse) → suggest retry/timeout config tweak
    * Slow tools beyond p95 threshold (Langfuse) → suggest timeout bump
    * Repeated-failure pattern in control-plane (Postgres) → suggest
      adding the failing tool to the watch-list or escalation policy
    * High meta-overturn rate (Postgres) → suggest tightening ideate
      prompt
    * Reverted PRs (GitHub) → flag for human review (no auto-fix)
    * Low merge rate (GitHub) → suggest reviewing CI flakiness

Each ``SuggestedFix`` carries a ``confidence`` (0..1) and a
``cheapness`` ('cheap' for config/prompt edits that the flywheel can
auto-PR with ``auto-merge-ok``; 'expensive' for logic edits that always
require human review).

The synthesizer never reads code or generates code; pr_generator does
that downstream using these fix specs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Cheapness = Literal["cheap", "expensive"]

# Defaults tuned for a 7-day window. Operators can override at call time.
HIGH_ERROR_RATE = 0.10  # 10%+ of calls error
SLOW_P95_MS = 30_000  # 30s p95 considered slow
HIGH_OVERTURN_RATE = 0.20  # 20%+ of decisions overturned by meta
LOW_MERGE_RATE = 0.50  # <50% PR merge rate is suspicious
REVERT_FLAG_THRESHOLD = 1  # any revert is worth surfacing
REPEATED_FAILURE_MIN = 3  # same error+tool >=3 times in window

# Confidence floors per heuristic — tuned conservative.
CONFIDENCE_HIGH_ERROR = 0.80
CONFIDENCE_SLOW = 0.65
CONFIDENCE_OVERTURN = 0.70
CONFIDENCE_REPEATED_FAILURE = 0.85
CONFIDENCE_LOW_MERGE = 0.55
CONFIDENCE_REVERT = 1.00  # human always reviews; not auto-fixable


@dataclass(frozen=True)
class SuggestedFix:
    title: str
    rationale: str
    confidence: float
    cheapness: Cheapness
    target: str  # short identifier (tool name, file, etc.)
    fix_kind: str  # 'config_tweak' | 'prompt_edit' | 'flag_for_review' | ...
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QualityReport:
    top_problems: list[str]
    suggested_fixes: list[SuggestedFix]
    confidence_score: float
    signals: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "top_problems": list(self.top_problems),
            "suggested_fixes": [f.as_dict() for f in self.suggested_fixes],
            "confidence_score": self.confidence_score,
            "signals": self.signals,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def synthesize(
    langfuse_signals: dict[str, Any],
    postgres_signals: dict[str, Any],
    github_signals: dict[str, Any],
    *,
    high_error_rate: float = HIGH_ERROR_RATE,
    slow_p95_ms: float = SLOW_P95_MS,
    high_overturn_rate: float = HIGH_OVERTURN_RATE,
    low_merge_rate: float = LOW_MERGE_RATE,
) -> QualityReport:
    """Combine the three signal blobs into a QualityReport.

    Each detector contributes 0+ SuggestedFixes. We dedupe by
    (target, fix_kind), keep the highest-confidence, then compute an
    overall ``confidence_score`` as the mean of contributing fixes.
    """
    fixes: list[SuggestedFix] = []
    fixes.extend(_langfuse_fixes(langfuse_signals, high_error_rate, slow_p95_ms))
    fixes.extend(_postgres_fixes(postgres_signals, high_overturn_rate))
    fixes.extend(_github_fixes(github_signals, low_merge_rate))

    fixes = _dedupe(fixes)

    top_problems = [f.title for f in sorted(fixes, key=lambda f: -f.confidence)[:3]]
    overall = (sum(f.confidence for f in fixes) / len(fixes)) if fixes else 0.0

    return QualityReport(
        top_problems=top_problems,
        suggested_fixes=fixes,
        confidence_score=overall,
        signals={
            "langfuse_configured": bool(langfuse_signals.get("configured")),
            "postgres_configured": bool(postgres_signals.get("configured")),
            "github_configured": bool(github_signals.get("configured")),
            "trace_count": int(langfuse_signals.get("trace_count", 0)),
            "pr_count_total": sum(
                r.get("pr_count", 0) for r in (github_signals.get("per_repo") or {}).values()
            ),
        },
    )


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def _langfuse_fixes(
    sig: dict[str, Any], high_error_rate: float, slow_p95_ms: float
) -> list[SuggestedFix]:
    if not sig.get("configured"):
        return []
    fixes: list[SuggestedFix] = []

    error_rate: dict[str, float] = sig.get("error_rate") or {}
    for tool, rate in error_rate.items():
        if rate >= high_error_rate:
            fixes.append(
                SuggestedFix(
                    title=f"Tool '{tool}' error rate {rate:.0%} (>= {high_error_rate:.0%})",
                    rationale=(
                        f"Last 7d Langfuse spans show {tool} failing at "
                        f"{rate:.0%}. Suggest enabling exponential backoff "
                        "and bumping retry from 1 to 3 in director config."
                    ),
                    confidence=CONFIDENCE_HIGH_ERROR,
                    cheapness="cheap",
                    target=tool,
                    fix_kind="config_tweak",
                    payload={"retry_count": 3, "backoff": "exponential"},
                )
            )

    latency: dict[str, dict[str, float]] = sig.get("per_tool_latency") or {}
    for tool, stats in latency.items():
        p95 = stats.get("p95", 0.0)
        if p95 >= slow_p95_ms:
            fixes.append(
                SuggestedFix(
                    title=f"Tool '{tool}' p95 latency {p95:.0f}ms (>= {slow_p95_ms:.0f}ms)",
                    rationale=(
                        f"{tool} is consistently slow. Suggest bumping its "
                        f"timeout configuration to 2x p95 ({2 * p95:.0f}ms)."
                    ),
                    confidence=CONFIDENCE_SLOW,
                    cheapness="cheap",
                    target=tool,
                    fix_kind="config_tweak",
                    payload={"timeout_ms": int(2 * p95)},
                )
            )

    return fixes


def _postgres_fixes(sig: dict[str, Any], high_overturn_rate: float) -> list[SuggestedFix]:
    if not sig.get("configured"):
        return []
    fixes: list[SuggestedFix] = []

    repeated = sig.get("repeated_failures") or []
    for entry in repeated:
        # Accept either {tool,error,count} (legacy) or {pattern,count}
        # (current postgres_signals shape).
        tool = entry.get("tool") or entry.get("pattern")
        error = entry.get("error") or entry.get("pattern")
        count = int(entry.get("count", 0))
        if not tool or count < REPEATED_FAILURE_MIN:
            continue
        fixes.append(
            SuggestedFix(
                title=f"Repeated failure: {tool} → {error!r} x{count}",
                rationale=(
                    f"Same failure surfaced {count}× in 7d. Suggest adding "
                    "an explicit error-handling branch in the relevant "
                    "agent OR escalating to a human via Telegram."
                ),
                confidence=CONFIDENCE_REPEATED_FAILURE,
                cheapness="expensive",  # logic change → human review
                target=str(tool),
                fix_kind="flag_for_review",
                payload={"error_signature": error, "occurrence_count": count},
            )
        )

    # Compute overturn rate from raw count when only count is available.
    overturn_rate = float(sig.get("decisions_overturned_rate") or 0.0)
    if overturn_rate == 0.0:
        count = int(sig.get("decisions_overturned_count") or 0)
        total = int(sig.get("task_count") or 0)
        if total > 0:
            overturn_rate = count / total
    if overturn_rate >= high_overturn_rate:
        fixes.append(
            SuggestedFix(
                title=f"Meta overturn rate {overturn_rate:.0%} (>= {high_overturn_rate:.0%})",
                rationale=(
                    f"Meta is overturning {overturn_rate:.0%} of ideate "
                    "decisions. Suggest tightening the ideate system prompt "
                    "to bake in meta's most-common corrections."
                ),
                confidence=CONFIDENCE_OVERTURN,
                cheapness="cheap",
                target="director.ideate",
                fix_kind="prompt_edit",
                payload={"overturn_rate": overturn_rate},
            )
        )

    return fixes


def _github_fixes(sig: dict[str, Any], low_merge_rate: float) -> list[SuggestedFix]:
    if not sig.get("configured"):
        return []
    fixes: list[SuggestedFix] = []

    merge_rate = float(sig.get("merge_rate") or 0.0)
    pr_count = sum(r.get("pr_count", 0) for r in (sig.get("per_repo") or {}).values())
    if pr_count >= 5 and merge_rate < low_merge_rate:  # noqa: PLR2004
        fixes.append(
            SuggestedFix(
                title=f"Fleet merge rate {merge_rate:.0%} (< {low_merge_rate:.0%})",
                rationale=(
                    f"Out of {pr_count} PRs in 7d only {merge_rate:.0%} got "
                    "merged. Suggest investigating CI flake (see "
                    "ci_flake_repos) and the auto-merge guard."
                ),
                confidence=CONFIDENCE_LOW_MERGE,
                cheapness="expensive",
                target="fleet",
                fix_kind="flag_for_review",
                payload={"merge_rate": merge_rate, "pr_count": pr_count},
            )
        )

    reverted = sig.get("reverted_pr_numbers") or []
    if len(reverted) >= REVERT_FLAG_THRESHOLD:
        fixes.append(
            SuggestedFix(
                title=f"Reverted PRs detected: {len(reverted)}",
                rationale=(
                    "At least one PR was merged then reverted. Always a "
                    "human-review signal — never auto-fixable."
                ),
                confidence=CONFIDENCE_REVERT,
                cheapness="expensive",
                target="fleet",
                fix_kind="flag_for_review",
                payload={"reverted": reverted},
            )
        )

    return fixes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dedupe(fixes: list[SuggestedFix]) -> list[SuggestedFix]:
    """Keep highest-confidence fix per (target, fix_kind)."""
    by_key: dict[tuple[str, str], SuggestedFix] = {}
    for f in fixes:
        key = (f.target, f.fix_kind)
        existing = by_key.get(key)
        if existing is None or f.confidence > existing.confidence:
            by_key[key] = f
    return sorted(by_key.values(), key=lambda f: -f.confidence)
