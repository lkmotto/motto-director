"""Apply northflank/crons.yaml to Northflank as cron jobs.

Idempotent: for each cron in the manifest, look up an existing job by
name; PATCH if present, POST if not.  Runs from the GitHub Action
``apply-crons.yaml`` whenever ``northflank/crons.yaml`` changes on
``main``, and is safe to run locally for verification.

Environment
-----------
- ``NORTHFLANK_API_KEY`` (or fallback ``NORTHFLANK_API_TOKEN``):
  Required.  Without it the script becomes a graceful no-op so unit
  tests / dry runs don't blow up.
- ``NORTHFLANK_PROJECT``: optional override; defaults to the
  ``project`` field in the manifest.
- ``DRY_RUN=1``: print intended writes without calling the API.

Usage
-----
::

    python scripts/apply_northflank_crons.py [path/to/crons.yaml]

Default manifest path is ``northflank/crons.yaml`` relative to repo root.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

logger = logging.getLogger("apply_northflank_crons")

NORTHFLANK_API = "https://api.northflank.com/v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def northflank_api_key() -> str:
    """Same precedence as director.perceive.northflank_api_key()."""
    return os.environ.get("NORTHFLANK_API_KEY") or os.environ.get(
        "NORTHFLANK_API_TOKEN", ""
    )


@dataclass(frozen=True)
class CronSpec:
    name: str
    schedule: str
    command: list[str]
    retries: int
    timeout_seconds: int


def load_manifest(path: Path) -> tuple[str, str, list[CronSpec]]:
    """Parse the YAML manifest into (project, image, [CronSpec, ...])."""
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"manifest {path} did not parse to a mapping")

    project = os.environ.get("NORTHFLANK_PROJECT") or raw.get("project")
    if not project:
        raise ValueError("manifest is missing 'project' and NORTHFLANK_PROJECT is unset")

    image = raw.get("image")
    if not image:
        raise ValueError("manifest is missing 'image'")

    defaults: dict[str, Any] = raw.get("defaults") or {}
    default_retries = int(defaults.get("retries", 1))
    default_timeout = int(defaults.get("timeout_seconds", 600))

    crons_raw = raw.get("crons") or []
    if not isinstance(crons_raw, list) or not crons_raw:
        raise ValueError("manifest must define a non-empty 'crons' list")

    specs: list[CronSpec] = []
    seen: set[str] = set()
    for entry in crons_raw:
        name = entry.get("name")
        schedule = entry.get("schedule")
        command = entry.get("command")
        if not (name and schedule and command):
            raise ValueError(f"cron entry missing required fields: {entry!r}")
        if name in seen:
            raise ValueError(f"duplicate cron name: {name}")
        seen.add(name)
        if not isinstance(command, list):
            raise ValueError(f"cron {name}: 'command' must be a list")
        specs.append(
            CronSpec(
                name=str(name),
                schedule=str(schedule),
                command=[str(c) for c in command],
                retries=int(entry.get("retries", default_retries)),
                timeout_seconds=int(entry.get("timeout_seconds", default_timeout)),
            )
        )

    return str(project), str(image), specs


# ---------------------------------------------------------------------------
# Northflank client (thin)
# ---------------------------------------------------------------------------


class NorthflankClient:
    """Tiny REST wrapper. Real network calls only when token is set."""

    def __init__(self, token: str, dry_run: bool = False) -> None:
        self._token = token
        self._dry = dry_run
        self._client = httpx.Client(timeout=30.0)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def list_jobs(self, project: str) -> list[dict[str, Any]]:
        if self._dry:
            return []
        r = self._client.get(
            f"{NORTHFLANK_API}/projects/{project}/jobs",
            headers=self._headers(),
        )
        r.raise_for_status()
        data = r.json().get("data") or {}
        return list(data.get("jobs") or [])

    def create_cron(
        self, project: str, image: str, spec: CronSpec
    ) -> dict[str, Any]:
        body = self._cron_body(image, spec)
        if self._dry:
            logger.info("DRY_RUN create %s/%s body=%s", project, spec.name, body)
            return {"id": spec.name, "_dry_run": True}
        r = self._client.post(
            f"{NORTHFLANK_API}/projects/{project}/jobs",
            headers=self._headers(),
            json=body,
        )
        r.raise_for_status()
        return r.json().get("data") or {}

    def update_cron(
        self, project: str, image: str, spec: CronSpec
    ) -> dict[str, Any]:
        body = self._cron_body(image, spec)
        if self._dry:
            logger.info("DRY_RUN update %s/%s body=%s", project, spec.name, body)
            return {"id": spec.name, "_dry_run": True}
        r = self._client.patch(
            f"{NORTHFLANK_API}/projects/{project}/jobs/{spec.name}",
            headers=self._headers(),
            json=body,
        )
        r.raise_for_status()
        return r.json().get("data") or {}

    @staticmethod
    def _cron_body(image: str, spec: CronSpec) -> dict[str, Any]:
        """Northflank cron-job create/update payload.

        Schema follows https://api.northflank.com/v1/docs#tag/Jobs (2026 API).
        Only the fields we actually own here are set; Northflank fills the
        rest from project defaults.
        """
        return {
            "name": spec.name,
            "description": f"motto-director cron: {spec.name}",
            "billing": {"deploymentPlan": "nf-compute-20"},
            "backoffLimit": spec.retries,
            "activeDeadlineSeconds": spec.timeout_seconds,
            "runOnSourceChange": "never",
            "schedule": {"crontab": spec.schedule, "timezone": "UTC"},
            "deployment": {
                "instances": 1,
                "internal": {
                    # Reuse the director image: the same combined-service
                    # build artifact that powers other director jobs.
                    "id": image,
                    "branch": "main",
                },
                "command": spec.command,
            },
        }


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply(manifest_path: Path) -> dict[str, str]:
    """Apply manifest. Returns {cron_name: action} where action is
    'created' | 'updated' | 'noop' | 'dry-run-create' | 'dry-run-update' | 'skipped'."""
    project, image, specs = load_manifest(manifest_path)
    token = northflank_api_key()
    dry_run = os.environ.get("DRY_RUN") == "1"

    if not token and not dry_run:
        logger.warning(
            "NORTHFLANK_API_KEY not set; skipping apply (returning 'skipped' for all)."
        )
        return {s.name: "skipped" for s in specs}

    client = NorthflankClient(token=token or "DRY", dry_run=dry_run)
    existing = {j.get("name"): j for j in client.list_jobs(project)}

    results: dict[str, str] = {}
    for spec in specs:
        if spec.name in existing:
            client.update_cron(project, image, spec)
            results[spec.name] = "dry-run-update" if dry_run else "updated"
        else:
            client.create_cron(project, image, spec)
            results[spec.name] = "dry-run-create" if dry_run else "created"
        logger.info("cron %s -> %s", spec.name, results[spec.name])

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        nargs="?",
        default=str(Path(__file__).resolve().parent.parent / "northflank" / "crons.yaml"),
        help="path to northflank/crons.yaml",
    )
    args = parser.parse_args(argv)
    try:
        results = apply(Path(args.manifest))
    except Exception as exc:  # noqa: BLE001
        logger.exception("apply failed: %s", exc)
        return 1
    for name, action in results.items():
        print(f"{name}\t{action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
