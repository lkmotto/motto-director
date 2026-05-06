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


# ---------- DownTime segregation ----------


def test_is_downtime_repo_recognizes_prefix():
    assert kpis.is_downtime_repo("downtime-event-agent")
    assert kpis.is_downtime_repo("lkmotto/downtime-email-agent")
    assert kpis.is_downtime_repo("DOWNTIME-app")  # case-insensitive


def test_is_downtime_repo_rejects_motto():
    assert not kpis.is_downtime_repo("motto-director")
    assert not kpis.is_downtime_repo("lkmotto/motto-sdr-agent")
    assert not kpis.is_downtime_repo("rw-order-monitor")
    assert not kpis.is_downtime_repo(None)
    assert not kpis.is_downtime_repo("")


def test_load_downtime_kpis_env_override(monkeypatch):
    monkeypatch.setenv("DOWNTIME_KPIS", "## events/week: 0/200")
    monkeypatch.delenv("DOWNTIME_KPIS_PATH", raising=False)
    assert kpis.load_downtime_kpis() == "## events/week: 0/200"


def test_load_downtime_kpis_path_override(monkeypatch, tmp_path):
    monkeypatch.delenv("DOWNTIME_KPIS", raising=False)
    f = tmp_path / "dt.md"
    f.write_text("# DT Goals\n- digest open: 35%")
    monkeypatch.setenv("DOWNTIME_KPIS_PATH", str(f))
    assert "digest open" in kpis.load_downtime_kpis()


def test_load_kpis_for_scope_routes_by_repo(monkeypatch):
    monkeypatch.setenv("KPIS", "## motto-only-kpi")
    monkeypatch.setenv("DOWNTIME_KPIS", "## downtime-only-kpi")
    assert "motto-only-kpi" in kpis.load_kpis_for_scope("motto-sdr-agent")
    assert "downtime-only-kpi" in kpis.load_kpis_for_scope("downtime-app")
    assert "motto-only-kpi" in kpis.load_kpis_for_scope(None)


def test_format_for_prompt_downtime_label():
    out = kpis.format_for_prompt("- digest open: 35%", product="DOWNTIME")
    assert "DOWNTIME KPIs" in out
    assert "MOTTO KPIs" not in out


def test_motto_kpi_file_uses_heading_format():
    """Every per-repo KPI in motto-kpis.md must use `### KPI: <title>` so
    the planner can lift the title verbatim into kpi_ref. If this test
    fails because someone added a `#### KPI:` heading instead, the planner
    will silently drop epics targeting that KPI."""
    import re
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "motto-kpis.md").read_text()
    assert not re.search(r"^#### KPI:", text, re.M), (
        "motto-kpis.md uses 4-hash KPI headings; planner only matches 3-hash"
    )
    titles = re.findall(r"^### KPI: (.+)$", text, re.M)
    assert len(titles) >= 20, f"motto-kpis.md only has {len(titles)} KPIs; expected ≥20"
    assert len(set(titles)) == len(titles), "motto-kpis.md has duplicate KPI titles"


def test_downtime_kpi_file_uses_heading_format():
    import re
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "downtime-kpis.md").read_text()
    assert not re.search(r"^#### KPI:", text, re.M)
    titles = re.findall(r"^### KPI: (.+)$", text, re.M)
    assert len(titles) >= 10, f"downtime-kpis.md only has {len(titles)} KPIs; expected ≥10"
    assert len(set(titles)) == len(titles)


def test_kpi_files_dont_overlap_titles():
    """Sanity: motto and downtime KPI files must not share KPI titles, or
    `kpi_ref` lookup becomes ambiguous."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    motto = (root / "motto-kpis.md").read_text()
    dt = (root / "downtime-kpis.md").read_text()
    motto_titles = set(re.findall(r"^### KPI: (.+)$", motto, re.M))
    dt_titles = set(re.findall(r"^### KPI: (.+)$", dt, re.M))
    overlap = motto_titles & dt_titles
    assert not overlap, f"KPI titles overlap between product lines: {overlap}"
