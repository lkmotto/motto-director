"""Tests for director.queue (pending_moves enqueue / approve / apply)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from director import queue
from director.ideate import NextMove

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _move(
    repo: str = "lkmotto/motto-director",
    kind: str = "file_issue",
    title: str = "fix CI flake",
    priority: int = 5,
    intent: str = "Stabilize CI",
) -> NextMove:
    return NextMove(
        repo=repo,
        kind=kind,
        title=title,
        rationale="rationale",
        prompt_for_claude_code="prompt",
        priority=priority,
        intent=intent,
        code_changes=[],
    )


def _patch_psycopg_cursor():
    """Build a psycopg mock whose cursor().execute is observable."""
    cur = MagicMock()
    cur.rowcount = 1
    cur_cm = MagicMock()
    cur_cm.__enter__.return_value = cur
    cur_cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cur_cm
    conn_cm = MagicMock()
    conn_cm.__enter__.return_value = conn
    conn_cm.__exit__.return_value = False
    fake_psycopg = MagicMock()
    fake_psycopg.connect.return_value = conn_cm
    # The errors namespace must exist for the import in queue.py.
    fake_psycopg.errors = MagicMock()
    fake_psycopg.errors.UniqueViolation = type("UniqueViolation", (Exception,), {})
    return fake_psycopg, conn, cur


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------


def test_manual_mode_off_by_default(monkeypatch):
    monkeypatch.delenv("DIRECTOR_APPROVAL_MODE", raising=False)
    assert queue.manual_mode_enabled() is False


def test_manual_mode_explicit_auto(monkeypatch):
    monkeypatch.setenv("DIRECTOR_APPROVAL_MODE", "auto")
    assert queue.manual_mode_enabled() is False


def test_manual_mode_on(monkeypatch):
    monkeypatch.setenv("DIRECTOR_APPROVAL_MODE", "manual")
    assert queue.manual_mode_enabled() is True


def test_manual_mode_case_insensitive(monkeypatch):
    monkeypatch.setenv("DIRECTOR_APPROVAL_MODE", "MANUAL")
    assert queue.manual_mode_enabled() is True


# ---------------------------------------------------------------------------
# enqueue_moves
# ---------------------------------------------------------------------------


def test_enqueue_no_dsn_returns_errors(monkeypatch):
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    out = queue.enqueue_moves([_move(), _move(title="other")], run_id="r1")
    assert out == {"queued": 0, "deduped": 0, "errors": 2}


def test_enqueue_writes_each_move(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://stub")
    fake_psycopg, conn, cur = _patch_psycopg_cursor()
    with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
        out = queue.enqueue_moves(
            [_move(title="A"), _move(title="B")],
            run_id="r1",
        )
    assert out == {"queued": 2, "deduped": 0, "errors": 0}
    # 2 INSERTs
    assert cur.execute.call_count == 2
    # Each commit was issued
    assert conn.commit.call_count == 2


def test_enqueue_dedup_on_unique_violation(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://stub")
    fake_psycopg, conn, cur = _patch_psycopg_cursor()
    UV = fake_psycopg.errors.UniqueViolation
    cur.execute.side_effect = [None, UV("dup")]
    with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
        out = queue.enqueue_moves(
            [_move(title="A"), _move(title="A")],
            run_id="r1",
        )
    assert out["queued"] == 1
    assert out["deduped"] == 1
    assert out["errors"] == 0


def test_enqueue_other_exception_counts_as_error(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://stub")
    fake_psycopg, conn, cur = _patch_psycopg_cursor()
    cur.execute.side_effect = [None, RuntimeError("boom")]
    with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
        out = queue.enqueue_moves(
            [_move(title="A"), _move(title="B")],
            run_id="r1",
        )
    assert out["queued"] == 1
    assert out["errors"] == 1


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def test_approve_returns_true_when_row_updated(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://stub")
    fake_psycopg, conn, cur = _patch_psycopg_cursor()
    cur.rowcount = 1
    with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
        ok = queue.approve(42, approved_by="cockpit:tok")
    assert ok is True


def test_approve_returns_false_when_no_row(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://stub")
    fake_psycopg, conn, cur = _patch_psycopg_cursor()
    cur.rowcount = 0
    with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
        ok = queue.approve(42, approved_by="cockpit:tok")
    assert ok is False


def test_reject_path_uses_pending_to_rejected(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://stub")
    fake_psycopg, conn, cur = _patch_psycopg_cursor()
    cur.rowcount = 1
    with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
        queue.reject(7, approved_by="telegram:123")
    # last execute call must transition pending->rejected
    sql, args = cur.execute.call_args[0]
    assert "rejected" in args
    assert "pending" in args
    assert 7 in args


def test_bulk_approve(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://stub")
    fake_psycopg, conn, cur = _patch_psycopg_cursor()
    cur.rowcount = 3
    with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
        n = queue.bulk_approve([1, 2, 3], approved_by="cockpit:tok")
    assert n == 3


def test_expire_stale(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://stub")
    fake_psycopg, conn, cur = _patch_psycopg_cursor()
    cur.rowcount = 4
    with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
        n = queue.expire_stale(48)
    assert n == 4


# ---------------------------------------------------------------------------
# row_to_move
# ---------------------------------------------------------------------------


def test_row_to_move_reconstructs_dataclass():
    row = {
        "id": 1,
        "repo": "lkmotto/motto-director",
        "kind": "file_issue",
        "title": "fix CI",
        "rationale": "r",
        "intent": "i",
        "priority": 7,
        "move_payload": {
            "repo": "lkmotto/motto-director",
            "kind": "file_issue",
            "title": "fix CI",
            "rationale": "r-from-payload",
            "prompt_for_claude_code": "p",
            "priority": 7,
            "intent": "i",
            "code_changes": [{"path": "x.py", "diff": "..."}],
        },
    }
    m = queue.row_to_move(row)
    assert m.repo == "lkmotto/motto-director"
    assert m.kind == "file_issue"
    assert m.priority == 7
    # payload wins where present (round-trip from enqueue)
    assert m.rationale == "r-from-payload"
    assert m.code_changes == [{"path": "x.py", "diff": "..."}]


def test_row_to_move_payload_as_string_is_parsed():
    import json as _json

    row = {
        "id": 1,
        "repo": "r",
        "kind": "k",
        "title": "t",
        "rationale": "",
        "intent": "",
        "priority": 0,
        "move_payload": _json.dumps(
            {
                "repo": "r",
                "kind": "k",
                "title": "t",
                "rationale": "",
                "prompt_for_claude_code": "",
                "priority": 0,
                "intent": "",
                "code_changes": [],
            }
        ),
    }
    m = queue.row_to_move(row)
    assert m.repo == "r"
    assert m.code_changes == []


# ---------------------------------------------------------------------------
# list helpers
# ---------------------------------------------------------------------------


def test_list_pending_no_dsn(monkeypatch):
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert queue.list_pending() == []
