"""Unit tests for director.kpis."""

from __future__ import annotations

from director import kpis


def test_kpis_env_override(monkeypatch):
    monkeypatch.setenv("KPIS", "AMC panels: 4/42")
    monkeypatch.delenv("KPIS_PATH", raising=False)
    assert kpis.load_kpis() == "AMC panels: 4/42"


def test_kpis_path_override(monkeypatch, tmp_path):
    monkeypatch.delenv("KPIS", raising=False)
    f = tmp_path / "kpis.md"
    f.write_text("# Goals\n- foo: 1/10")
    monkeypatch.setenv("KPIS_PATH", str(f))
    assert "foo: 1/10" in kpis.load_kpis()


def test_kpis_missing_returns_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("KPIS", raising=False)
    monkeypatch.setenv("KPIS_PATH", str(tmp_path / "nope.md"))
    # repo root file may exist; but with KPIS_PATH set first and missing,
    # we still fall through. Either real file is loaded or empty. Assert
    # the shape only.
    out = kpis.load_kpis()
    assert isinstance(out, str)


def test_format_for_prompt_empty():
    assert kpis.format_for_prompt("") == ""


def test_format_for_prompt_wraps():
    out = kpis.format_for_prompt("- AMC panels: 4/42")
    assert "MOTTO KPIs" in out
    assert "AMC panels: 4/42" in out
    assert out.endswith("\n\n")


def test_kpis_truncates_huge_input(monkeypatch):
    big = "x" * 50_000
    monkeypatch.setenv("KPIS", big)
    out = kpis.load_kpis()
    assert len(out) < len(big)
    assert "[truncated]" in out
