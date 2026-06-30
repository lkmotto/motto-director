#!/usr/bin/env python3
"""Configure Sentry alert rules for the Motto fleet.

Idempotent: checks for existing rules by name before creating.
Retries transient failures (429, 5xx) with exponential backoff.

Usage:
    SENTRY_AUTH_TOKEN=<token> python scripts/configure_sentry_alerts.py

The SENTRY_AUTH_TOKEN must have ``alerts:write`` scope.
Set ``SENTRY_ORG`` (default: ``lkmotto``) and ``SENTRY_PROJECT``
(default: ``motto-director``) to override the Sentry org/project.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request
from json import JSONDecodeError, dumps, loads
from typing import Any

# ---------------------------------------------------------------------------
# Configuration (override via environment)
# ---------------------------------------------------------------------------
SENTRY_ORG = os.getenv("SENTRY_ORG", "lkmotto")
SENTRY_PROJECT = os.getenv("SENTRY_PROJECT", "motto-director")
SENTRY_BASE = os.getenv("SENTRY_BASE_URL", "https://sentry.io")
SENTRY_TOKEN = os.getenv("SENTRY_AUTH_TOKEN", "")

# Notification target: team slug or numeric team ID, or email address.
# The alert will send email notifications to this target via the built-in
# Sentry mail action.  Override with SENTRY_ALERT_TARGET (e.g. a Slack
# integration ID when you switch to Slack).
SENTRY_ALERT_TARGET = os.getenv("SENTRY_ALERT_TARGET", "lkmotto")

API = f"{SENTRY_BASE}/api/0/projects/{SENTRY_ORG}/{SENTRY_PROJECT}/rules"

# ---------------------------------------------------------------------------
# Alert rule definitions
# ---------------------------------------------------------------------------

ALERT_RULES: list[dict[str, Any]] = [
    {
        "name": "High Error Rate (50+/hour)",
        "actionMatch": "all",
        "actions": [
            {
                "id": "sentry.mail.actions.EmailNotifyEmailAction",
                "targetType": "team",
                "targetIdentifier": SENTRY_ALERT_TARGET,
            }
        ],
        "conditions": [
            {
                "id": "sentry.rules.conditions.event_frequency.EventFrequencyCondition",
                "interval": "1h",
                "value": 50,
            }
        ],
        "filterMatch": "all",
        "filters": [
            {
                "id": "sentry.rules.filters.level.LevelFilter",
                "level": "40",
                "match": "gte",
            }
        ],
        "frequency": 5,
        "environment": "production",
    },
    {
        "name": "Issue Frequency Spike",
        "actionMatch": "all",
        "actions": [
            {
                "id": "sentry.mail.actions.EmailNotifyEmailAction",
                "targetType": "team",
                "targetIdentifier": SENTRY_ALERT_TARGET,
            }
        ],
        "conditions": [
            {
                "id": "sentry.rules.conditions.event_frequency.EventFrequencyPercentCondition",
                "interval": "1h",
                "value": 300,
                "comparisonInterval": "1h",
                "comparisonType": "percent",
            }
        ],
        "filterMatch": "all",
        "filters": [],
        "frequency": 5,
        "environment": "production",
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _api(
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    max_retries: int = 3,
) -> tuple[int, dict[str, Any] | list[Any] | None]:
    """Wrapper around Sentry HTTP API with retry resiliency."""
    url = f"{endpoint}"
    body: bytes | None = dumps(data).encode("utf-8") if data is not None else None
    headers = {
        "Authorization": f"Bearer {SENTRY_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
                result: dict[str, Any] | list[Any] | None = (
                    loads(resp.read().decode())
                    if resp.status != 204
                    else None
                )
                return resp.status, result
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status == 429:
                retry_after = exc.headers.get("Retry-After", "5")
                wait = int(retry_after) if retry_after.isdigit() else 5
                print(f"  Rate limited (429). Waiting {wait}s (attempt {attempt}/{max_retries})...")
                time.sleep(wait)
                continue
            if 500 <= status < 600:
                wait = 2**attempt
                print(f"  Server error ({status}). Retrying in {wait}s (attempt {attempt}/{max_retries})...")
                time.sleep(wait)
                continue
            # 4xx (except 429): no retry
            try:
                detail = loads(exc.read().decode())
            except Exception:
                detail = str(exc)
            print(f"  Sentry API error ({status}): {detail}")
            return status, None
        except (urllib.error.URLError, OSError) as exc:
            wait = 2**attempt
            print(f"  Connection error: {exc}. Retrying in {wait}s (attempt {attempt}/{max_retries})...")
            time.sleep(wait)
            continue
    print(f"  Max retries ({max_retries}) exhausted.")
    return 0, None


def _existing_rule_names() -> set[str]:
    """Return the set of alert rule names already configured on the project."""
    status, result = _api(API)
    if status != 200 or not isinstance(result, list):
        print(f"  Could not list rules (status {status}), assuming empty.")
        return set()
    return {r["name"] for r in result if isinstance(r, dict) and "name" in r}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    if not SENTRY_TOKEN:
        print(
            "ERROR: SENTRY_AUTH_TOKEN is not set.\n"
            "  Export a Sentry auth token with alerts:write scope:\n"
            "    $env:SENTRY_AUTH_TOKEN = '<token>'  # PowerShell\n"
            "    export SENTRY_AUTH_TOKEN='<token>'  # bash\n"
        )
        return 1

    print(f"Sentry API: {SENTRY_BASE}")
    print(f"Org:        {SENTRY_ORG}")
    print(f"Project:    {SENTRY_PROJECT}")
    print()

    existing = _existing_rule_names()
    print(f"Existing rules ({len(existing)}): {', '.join(sorted(existing)) if existing else 'none'}")

    created = 0
    skipped = 0
    for rule in ALERT_RULES:
        name = rule["name"]
        if name in existing:
            print(f"  SKIP  {name}  (already exists)")
            skipped += 1
            continue

        status, _result = _api(API, method="POST", data=rule)
        if status == 201:
            print(f"  CREATE  {name}")
            created += 1
        else:
            print(f"  FAIL  {name}  (HTTP {status})")

        # Respect rate limits between creates
        time.sleep(0.5)

    print()
    print(f"Done. Created: {created}, skipped: {skipped}, total defined: {len(ALERT_RULES)}")
    return 0 if created + skipped == len(ALERT_RULES) else 1


if __name__ == "__main__":
    sys.exit(main())
