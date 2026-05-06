"""Unit tests for strategy.py + deep_read.py level switching."""

from __future__ import annotations

from director import deep_read, strategy


def test_strategy_env_override(monkeypatch):
    monkeypatch.setenv("STRATEGIC_INTENT", "north star: ship faster")
    monkeypatch.delenv("STRATEGIC_INTENT_PATH", raising=False)
    assert strategy.load_strategic_intent() == "north star: ship faster"


def test_strategy_format_for_prompt_empty_returns_empty():
    assert strategy.format_for_prompt("") == ""


def test_strategy_format_for_prompt_wraps_content():
    out = strategy.format_for_prompt("goal: x")
    assert "STRATEGIC INTENT" in out
    assert "goal: x" in out
    assert out.endswith("\n\n")


def test_deep_read_off_by_default(monkeypatch):
    monkeypatch.delenv("DIRECTOR_DEEP_READ_LEVEL", raising=False)
    assert not deep_read.is_enabled()


def test_deep_read_levels(monkeypatch):
    for level in ("light", "medium", "heavy"):
        monkeypatch.setenv("DIRECTOR_DEEP_READ_LEVEL", level)
        assert deep_read.is_enabled()
        assert deep_read._level() == level

    monkeypatch.setenv("DIRECTOR_DEEP_READ_LEVEL", "off")
    assert not deep_read.is_enabled()


def test_deep_read_budget_shapes():
    light = deep_read._budget("light")
    medium = deep_read._budget("medium")
    heavy = deep_read._budget("heavy")
    assert light["pr_diffs"] < medium["pr_diffs"] or light["pr_diffs"] == medium["pr_diffs"]
    assert medium["claude_md"] == 1
    assert heavy["commits"] > medium["commits"]
    assert heavy["pr_files_full"] > 0
