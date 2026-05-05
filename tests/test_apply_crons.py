"""Tests for scripts/apply_northflank_crons.py."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml

# Make scripts/ importable
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import apply_northflank_crons as anc  # noqa: E402


# ---------------------------------------------------------------------------
# load_manifest
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "crons.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def test_load_manifest_parses_defaults_and_specs(tmp_path: Path) -> None:
    p = _write_manifest(
        tmp_path,
        {
            "project": "motto-agents",
            "image": "motto-director",
            "defaults": {"retries": 1, "timeout_seconds": 600},
            "crons": [
                {
                    "name": "perceive",
                    "schedule": "*/15 * * * *",
                    "command": ["python", "-m", "director.cli", "perceive"],
                },
                {
                    "name": "meta",
                    "schedule": "0 9 * * 1",
                    "command": ["python", "-m", "director.cli", "meta"],
                    "retries": 0,
                    "timeout_seconds": 1800,
                },
            ],
        },
    )
    project, image, specs = anc.load_manifest(p)
    assert project == "motto-agents"
    assert image == "motto-director"
    assert [s.name for s in specs] == ["perceive", "meta"]
    assert specs[0].retries == 1
    assert specs[0].timeout_seconds == 600
    assert specs[1].retries == 0
    assert specs[1].timeout_seconds == 1800


def test_load_manifest_rejects_duplicate_names(tmp_path: Path) -> None:
    p = _write_manifest(
        tmp_path,
        {
            "project": "p",
            "image": "i",
            "crons": [
                {"name": "a", "schedule": "* * * * *", "command": ["x"]},
                {"name": "a", "schedule": "* * * * *", "command": ["x"]},
            ],
        },
    )
    with pytest.raises(ValueError, match="duplicate"):
        anc.load_manifest(p)


def test_load_manifest_rejects_missing_project(tmp_path: Path) -> None:
    p = _write_manifest(
        tmp_path,
        {"image": "i", "crons": [{"name": "a", "schedule": "* * * * *", "command": ["x"]}]},
    )
    with pytest.raises(ValueError, match="project"):
        anc.load_manifest(p)


def test_load_manifest_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write_manifest(
        tmp_path,
        {
            "project": "in-yaml",
            "image": "i",
            "crons": [{"name": "a", "schedule": "* * * * *", "command": ["x"]}],
        },
    )
    monkeypatch.setenv("NORTHFLANK_PROJECT", "from-env")
    project, _, _ = anc.load_manifest(p)
    assert project == "from-env"


# ---------------------------------------------------------------------------
# apply() — idempotent behavior
# ---------------------------------------------------------------------------


def _good_manifest(tmp_path: Path) -> Path:
    return _write_manifest(
        tmp_path,
        {
            "project": "motto-agents",
            "image": "motto-director",
            "crons": [
                {
                    "name": "director-perceive",
                    "schedule": "*/15 * * * *",
                    "command": ["python", "-m", "director.cli", "perceive"],
                },
                {
                    "name": "director-meta",
                    "schedule": "0 9 * * 1",
                    "command": ["python", "-m", "director.cli", "meta"],
                },
            ],
        },
    )


def test_apply_skips_when_no_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NORTHFLANK_API_KEY", raising=False)
    monkeypatch.delenv("NORTHFLANK_API_TOKEN", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    results = anc.apply(_good_manifest(tmp_path))
    assert set(results.values()) == {"skipped"}


def test_apply_creates_when_no_existing_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NORTHFLANK_API_KEY", "test-token")
    monkeypatch.delenv("DRY_RUN", raising=False)
    with mock.patch.object(anc.NorthflankClient, "list_jobs", return_value=[]) as m_list:
        with mock.patch.object(anc.NorthflankClient, "create_cron") as m_create:
            with mock.patch.object(anc.NorthflankClient, "update_cron") as m_update:
                results = anc.apply(_good_manifest(tmp_path))
    assert m_list.call_count == 1
    assert m_create.call_count == 2
    assert m_update.call_count == 0
    assert all(v == "created" for v in results.values())


def test_apply_updates_when_jobs_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NORTHFLANK_API_KEY", "test-token")
    monkeypatch.delenv("DRY_RUN", raising=False)
    existing = [{"name": "director-perceive"}, {"name": "director-meta"}]
    with mock.patch.object(anc.NorthflankClient, "list_jobs", return_value=existing):
        with mock.patch.object(anc.NorthflankClient, "create_cron") as m_create:
            with mock.patch.object(anc.NorthflankClient, "update_cron") as m_update:
                results = anc.apply(_good_manifest(tmp_path))
    assert m_create.call_count == 0
    assert m_update.call_count == 2
    assert all(v == "updated" for v in results.values())


def test_apply_mixed_create_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NORTHFLANK_API_KEY", "test-token")
    monkeypatch.delenv("DRY_RUN", raising=False)
    existing = [{"name": "director-perceive"}]  # meta missing
    with mock.patch.object(anc.NorthflankClient, "list_jobs", return_value=existing):
        with mock.patch.object(anc.NorthflankClient, "create_cron") as m_create:
            with mock.patch.object(anc.NorthflankClient, "update_cron") as m_update:
                results = anc.apply(_good_manifest(tmp_path))
    assert m_create.call_count == 1
    assert m_update.call_count == 1
    assert results["director-perceive"] == "updated"
    assert results["director-meta"] == "created"


def test_apply_dry_run_makes_no_real_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NORTHFLANK_API_KEY", raising=False)
    monkeypatch.delenv("NORTHFLANK_API_TOKEN", raising=False)
    monkeypatch.setenv("DRY_RUN", "1")
    results = anc.apply(_good_manifest(tmp_path))
    # In dry-run we still go through list_jobs (returns []) and "create"
    assert all(v.startswith("dry-run") for v in results.values())


# ---------------------------------------------------------------------------
# CronSpec body
# ---------------------------------------------------------------------------


def test_cron_body_has_required_fields() -> None:
    spec = anc.CronSpec(
        name="a",
        schedule="*/5 * * * *",
        command=["python", "-c", "1"],
        retries=2,
        timeout_seconds=120,
    )
    body = anc.NorthflankClient._cron_body("img", spec)
    assert body["name"] == "a"
    assert body["schedule"]["crontab"] == "*/5 * * * *"
    assert body["schedule"]["timezone"] == "UTC"
    assert body["backoffLimit"] == 2
    assert body["activeDeadlineSeconds"] == 120
    assert body["deployment"]["command"] == ["python", "-c", "1"]
    assert body["deployment"]["internal"]["id"] == "img"
