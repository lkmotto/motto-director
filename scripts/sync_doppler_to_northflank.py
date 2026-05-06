"""Sync Doppler motto-core/prd → Northflank motto-core-prd secret group.

Replaces the laptop-bound `doppler_to_northflank.sh` ritual. Designed to
run unattended in GitHub Actions, triggered by:

  - Doppler webhook → repository_dispatch (fully automatic)
  - workflow_dispatch (manual button)
  - cron (safety net)

Why a real Python script and not a shell one-liner: every prior shell
attempt has been a single PATCH that wiped sibling keys (motto-core-prd
postmortem, 2026-05-06). This script reads the current Northflank state,
diffs it against Doppler, computes a *full* merged variables map, and
sends ONE PATCH containing every key — never a partial set.

Inputs (env):
  DOPPLER_TOKEN          : Doppler service token (project-scoped, read-only)
  NORTHFLANK_API_KEY     : Northflank PAT
  DRY_RUN                : "1" → log diff, no PATCH (default off in CI)

Side-effects:
  - PATCH motto-agents/secrets/motto-core-prd with the merged variables
  - PATCH restrictions to ensure motto-mcp-server (svc) + motto-director (job)
    are linked
  - Print a redacted diff summary to stdout

The script *never* echoes secret values — only key names, lengths, and
whether each key was added/changed/unchanged/dropped.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ID = "motto-agents"
GROUP_ID = "motto-core-prd"
LINK_SVC = "motto-mcp-server"
LINK_JOB = "motto-director"
MCP_URL = "https://p01--motto-mcp-server--hq2dk45g4bfc.code.run"

# Keys we strip from the Doppler dump before pushing — these are
# Doppler's own metadata, not real secrets.
DOPPLER_META = ("DOPPLER_PROJECT", "DOPPLER_CONFIG", "DOPPLER_ENVIRONMENT")

# Hard floor: if the merged set is smaller than this, abort. Empirical
# floor based on what motto-core/prd is supposed to contain. Bump as
# the canonical set grows.
MIN_KEYS_FLOOR = 15


def fail(msg: str, code: int = 1) -> None:
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(code)


def http(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except HTTPError as e:  # noqa: PERF203
        body_txt = e.read().decode(errors="replace") if e.fp else ""
        fail(f"{method} {url} -> HTTP {e.code}: {body_txt[:500]}")
    except URLError as e:
        fail(f"{method} {url} -> URLError: {e.reason}")
    return {}  # unreachable


def fetch_doppler(token: str) -> dict[str, str]:
    """Fetch all secrets from Doppler motto-core/prd via API.

    Doppler API: https://docs.doppler.com/reference/secrets-download
    Service token auth: service tokens are pre-scoped to one project/config,
    no project/config args needed in URL.
    """
    url = "https://api.doppler.com/v3/configs/config/secrets/download?format=json"
    req = Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except HTTPError as e:
        body_txt = e.read().decode(errors="replace") if e.fp else ""
        fail(f"Doppler GET -> HTTP {e.code}: {body_txt[:500]}")
    except URLError as e:
        fail(f"Doppler GET -> URLError: {e.reason}")
    if not isinstance(data, dict):
        fail("Doppler returned non-dict payload")
    # Drop Doppler metadata
    for k in DOPPLER_META:
        data.pop(k, None)
    # Force every value to string (Doppler returns native types sometimes)
    return {k: str(v) for k, v in data.items() if v is not None}


def fetch_northflank_group(token: str) -> tuple[dict[str, str], dict[str, Any]]:
    resp = http(
        "GET",
        f"https://api.northflank.com/v1/projects/{PROJECT_ID}/secrets/{GROUP_ID}",
        token,
    )
    data = resp.get("data") or {}
    variables = (data.get("secrets") or {}).get("variables") or {}
    restrictions = data.get("restrictions") or {}
    return {k: str(v) for k, v in variables.items()}, restrictions


def inject_runtime(merged: dict[str, str]) -> dict[str, str]:
    """Inject keys that are computed, not stored in Doppler."""
    merged["MOTTO_MCP_URL"] = MCP_URL
    # Build OTEL header from Langfuse keys if present and non-placeholder
    pub = merged.get("LANGFUSE_PUBLIC_KEY", "")
    sec = merged.get("LANGFUSE_SECRET_KEY", "")
    if pub and sec and "TODO" not in pub and "TODO" not in sec:
        basic = base64.b64encode(f"{pub}:{sec}".encode()).decode()
        merged["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic%20{basic}"
    return merged


def diff_summary(old: dict[str, str], new: dict[str, str]) -> dict[str, list[str]]:
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in (set(old) & set(new)) if old[k] != new[k])
    unchanged = sorted(k for k in (set(old) & set(new)) if old[k] == new[k])
    return {"added": added, "removed": removed, "changed": changed, "unchanged": unchanged}


def main() -> None:
    doppler_tok = os.environ.get("DOPPLER_TOKEN", "").strip()
    nf_tok = os.environ.get("NORTHFLANK_API_KEY", "").strip()
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")

    if not doppler_tok:
        fail("DOPPLER_TOKEN missing")
    if not nf_tok:
        fail("NORTHFLANK_API_KEY missing")

    print("[1/4] Fetching Doppler motto-core/prd ...")
    doppler = fetch_doppler(doppler_tok)
    print(f"      Doppler returned {len(doppler)} variables")

    print(f"[2/4] Fetching Northflank {PROJECT_ID}/{GROUP_ID} current state ...")
    nf_current, nf_restrictions = fetch_northflank_group(nf_tok)
    print(f"      Northflank currently has {len(nf_current)} variables")

    # Build the merged set:
    # - Doppler is source of truth for every key it has
    # - Anything Northflank had but Doppler doesn't → DROP (the postmortem
    #   showed orphan keys persist forever otherwise)
    # - Then inject runtime-only keys (MOTTO_MCP_URL, OTEL headers)
    merged = dict(doppler)
    merged = inject_runtime(merged)

    # Safety floor
    if len(merged) < MIN_KEYS_FLOOR:
        fail(
            f"merged set has only {len(merged)} keys "
            f"(< floor {MIN_KEYS_FLOOR}); refusing to PATCH"
        )

    diff = diff_summary(nf_current, merged)
    print("[3/4] Diff vs current Northflank:")
    print(f"      + added   ({len(diff['added'])}): {diff['added']}")
    print(f"      - removed ({len(diff['removed'])}): {diff['removed']}")
    print(f"      ~ changed ({len(diff['changed'])}): {diff['changed']}")
    print(f"      = unchanged ({len(diff['unchanged'])})")

    if dry_run:
        print("[4/4] DRY_RUN=1 — skipping PATCH")
        return

    # Build payload — full set, never partial. Files preserved as empty
    # dict (we don't sync files via this path; if anything ever needs
    # them, add a fetch step here).
    payload = {"secrets": {"variables": merged, "files": {}}}
    print(f"[4/4] PATCHing {GROUP_ID} with {len(merged)} variables ...")
    resp = http(
        "PATCH",
        f"https://api.northflank.com/v1/projects/{PROJECT_ID}/secrets/{GROUP_ID}",
        nf_tok,
        body=payload,
    )
    after = (resp.get("data", {}).get("secrets", {}) or {}).get("variables", {}) or {}
    print(f"      Northflank now reports {len(after)} variables")
    if len(after) != len(merged):
        fail(
            f"post-PATCH count {len(after)} != expected {len(merged)} "
            f"(set drift; investigate immediately)"
        )

    # Ensure restrictions still link the right service+job. We DO NOT
    # widen restrictions here — adding more agents to motto-core-prd
    # belongs in a separate PR with explicit review.
    expected_objs = sorted(
        [(LINK_SVC, "service"), (LINK_JOB, "job")]
    )
    current_objs = sorted(
        [(o.get("id"), o.get("type")) for o in (nf_restrictions.get("nfObjects") or [])]
    )
    if not nf_restrictions.get("restricted") or current_objs != expected_objs:
        print(
            f"      restrictions need re-link "
            f"(current={current_objs}, expected={expected_objs}) ..."
        )
        link_payload = {
            "restrictions": {
                "restricted": True,
                "nfObjects": [
                    {"id": LINK_SVC, "type": "service"},
                    {"id": LINK_JOB, "type": "job"},
                ],
            }
        }
        http(
            "PATCH",
            f"https://api.northflank.com/v1/projects/{PROJECT_ID}/secrets/{GROUP_ID}",
            nf_tok,
            body=link_payload,
        )
        print("      restrictions re-linked")
    else:
        print("      restrictions already correct")

    print("DONE.")


if __name__ == "__main__":
    main()
