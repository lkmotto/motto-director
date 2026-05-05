"""Langfuse signals: per-tool latency p50/p95/p99, error rate, cost, error chains.

Reuses fleet.langfuse_recent_traces() which already wraps the Langfuse REST
API with bearer auth and graceful degradation when LANGFUSE_PUBLIC_KEY /
LANGFUSE_SECRET_KEY are unset. We then stitch trace data into the per-tool
percentile + error-pattern shape the synthesizer expects.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import Any

from director import fleet

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")
    )


async def collect(since_days: int = 7) -> dict[str, Any]:
    """Return a stable-shape signal blob.

    Empty when Langfuse isn't configured or returns nothing — caller MUST
    treat empty as a degraded signal, not an error.
    """
    if not is_configured():
        logger.info("langfuse_signals: not configured, returning empty")
        return _empty()

    since_minutes = since_days * 24 * 60
    traces = await fleet.langfuse_recent_traces(
        since_minutes=since_minutes,
        agent_name=None,
        limit=1000,
    )
    if not traces:
        return _empty()

    return {
        "configured": True,
        "trace_count": len(traces),
        "per_tool_latency": _per_tool_latency(traces),
        "error_rate": _error_rate(traces),
        "cost_per_decision_usd": _cost_per_decision(traces),
        "error_sequences": _error_sequences(traces),
    }


def _empty() -> dict[str, Any]:
    return {
        "configured": False,
        "trace_count": 0,
        "per_tool_latency": {},
        "error_rate": {},
        "cost_per_decision_usd": 0.0,
        "error_sequences": [],
    }


def _per_tool_latency(traces: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Per-tool p50 / p95 / p99 latency in ms, derived from trace observations."""
    durations: dict[str, list[float]] = defaultdict(list)
    for t in traces:
        for obs in t.get("observations") or []:
            tool = obs.get("name") or t.get("name") or "unknown"
            ms = _duration_ms(obs)
            if ms is not None:
                durations[tool].append(ms)
    out: dict[str, dict[str, float]] = {}
    for tool, samples in durations.items():
        if not samples:
            continue
        out[tool] = {
            "count": float(len(samples)),
            "p50": _percentile(samples, 50),
            "p95": _percentile(samples, 95),
            "p99": _percentile(samples, 99),
        }
    return out


def _error_rate(traces: list[dict[str, Any]]) -> dict[str, float]:
    """Error rate per tool: 0.0..1.0."""
    totals: dict[str, int] = defaultdict(int)
    errors: dict[str, int] = defaultdict(int)
    for t in traces:
        for obs in t.get("observations") or []:
            tool = obs.get("name") or t.get("name") or "unknown"
            totals[tool] += 1
            if obs.get("level") == "ERROR" or obs.get("statusMessage", "").lower().startswith(
                ("error", "fail")
            ):
                errors[tool] += 1
    return {tool: errors[tool] / totals[tool] for tool in totals if totals[tool] > 0}


def _cost_per_decision(traces: list[dict[str, Any]]) -> float:
    """Mean total-cost per trace in USD. 0.0 when no cost fields present."""
    costs = [
        float(t.get("totalCost") or 0)
        for t in traces
        if t.get("totalCost") is not None
    ]
    return sum(costs) / len(costs) if costs else 0.0


def _error_sequences(traces: list[dict[str, Any]]) -> list[list[str]]:
    """Tool-call sequences that ended in an error. Up to 20, deduped."""
    seqs: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for t in traces:
        obs_list = t.get("observations") or []
        if not obs_list:
            continue
        last = obs_list[-1]
        ended_error = (
            last.get("level") == "ERROR"
            or "error" in (last.get("statusMessage") or "").lower()
        )
        if not ended_error:
            continue
        chain = tuple((o.get("name") or "?") for o in obs_list[-5:])
        if chain in seen:
            continue
        seen.add(chain)
        seqs.append(chain)
        if len(seqs) >= 20:
            break
    return [list(c) for c in seqs]


def _duration_ms(obs: dict[str, Any]) -> float | None:
    start = obs.get("startTime")
    end = obs.get("endTime")
    if not start or not end:
        return None
    try:
        from datetime import datetime
        s = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        e = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        return (e - s).total_seconds() * 1000.0
    except (ValueError, TypeError):
        return None


def _percentile(samples: list[float], pct: int) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    k = max(0, min(len(s) - 1, int(round(pct / 100.0 * (len(s) - 1)))))
    return s[k]
