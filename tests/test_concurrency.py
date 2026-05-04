"""Tests for director.concurrency.adaptive_session_limit."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from director import concurrency


def test_no_dsn_falls_back_to_env_default(monkeypatch):
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DIRECTOR_MAX_CONCURRENT_SESSIONS", raising=False)
    assert concurrency.adaptive_session_limit() == 3


def test_no_dsn_respects_explicit_env(monkeypatch):
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DIRECTOR_MAX_CONCURRENT_SESSIONS", "5")
    assert concurrency.adaptive_session_limit() == 5


def _patch_psycopg(rows):
    """Build a context-manager mock that returns `rows` from fetchone()."""
    cur = MagicMock()
    cur.fetchone.return_value = rows
    cur_cm = MagicMock()
    cur_cm.__enter__.return_value = cur
    cur_cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cur_cm
    conn_cm = MagicMock()
    conn_cm.__enter__.return_value = conn
    conn_cm.__exit__.return_value = False
    return conn_cm


def test_scale_up_when_success_rate_high():
    fake_psycopg = MagicMock()
    fake_psycopg.connect.return_value = _patch_psycopg([18.0, 20.0])  # 90%
    with patch.dict("sys.modules", {"psycopg": fake_psycopg}):
        assert concurrency.adaptive_session_limit("postgres://x") == concurrency.MAX_LIMIT


def test_scale_down_when_success_rate_low():
    fake_psycopg = MagicMock()
    fake_psycopg.connect.return_value = _patch_psycopg([10.0, 20.0])  # 50%
    with patch.dict("sys.modules", {"psycopg": fake_psycopg}):
        assert concurrency.adaptive_session_limit("postgres://x") == concurrency.BASE_LIMIT


def test_middle_band_yields_baseline_plus_one():
    fake_psycopg = MagicMock()
    fake_psycopg.connect.return_value = _patch_psycopg([14.0, 20.0])  # 70%
    with patch.dict("sys.modules", {"psycopg": fake_psycopg}):
        assert (
            concurrency.adaptive_session_limit("postgres://x")
            == concurrency.BASE_LIMIT + 1
        )


def test_no_data_falls_back_to_baseline():
    fake_psycopg = MagicMock()
    fake_psycopg.connect.return_value = _patch_psycopg([0.0, 0.0])
    with patch.dict("sys.modules", {"psycopg": fake_psycopg}):
        assert concurrency.adaptive_session_limit("postgres://x") == concurrency.BASE_LIMIT


def test_too_few_runs_stay_at_baseline():
    fake_psycopg = MagicMock()
    fake_psycopg.connect.return_value = _patch_psycopg([2.0, 2.0])  # 100% but tiny
    with patch.dict("sys.modules", {"psycopg": fake_psycopg}):
        assert concurrency.adaptive_session_limit("postgres://x") == concurrency.BASE_LIMIT


def test_psycopg_failure_falls_back_to_baseline():
    fake_psycopg = MagicMock()
    fake_psycopg.connect.side_effect = RuntimeError("connection refused")
    with patch.dict("sys.modules", {"psycopg": fake_psycopg}):
        assert concurrency.adaptive_session_limit("postgres://x") == concurrency.BASE_LIMIT
