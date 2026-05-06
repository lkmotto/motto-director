"""Unit tests for director.repo_cache.

Network-free: tests focus on path safety, listing, log parsing — anything
that can be tested against a fake clone directory without invoking git.
"""

from __future__ import annotations

from pathlib import Path

from director.repo_cache import RepoCache


def _make_fake_clone(base: Path, repo_slug: str) -> Path:
    owner, name = repo_slug.split("/")
    path = base / owner / name
    path.mkdir(parents=True)
    (path / ".git").mkdir()
    return path


def test_repo_path(tmp_path):
    cache = RepoCache(base_dir=tmp_path)
    assert cache.repo_path("lkmotto/motto-director") == tmp_path / "lkmotto" / "motto-director"


def test_read_file_missing_returns_empty(tmp_path):
    cache = RepoCache(base_dir=tmp_path)
    assert cache.read_file("lkmotto/missing", "README.md") == ""


def test_read_file_in_clone(tmp_path):
    cache = RepoCache(base_dir=tmp_path)
    path = _make_fake_clone(tmp_path, "lkmotto/sample")
    (path / "README.md").write_text("hello\n")
    out = cache.read_file("lkmotto/sample", "README.md")
    assert out == "hello\n"


def test_read_file_path_escape_blocked(tmp_path):
    cache = RepoCache(base_dir=tmp_path)
    _make_fake_clone(tmp_path, "lkmotto/sample")
    # write a file outside the clone
    (tmp_path / "secret.txt").write_text("nope")
    assert cache.read_file("lkmotto/sample", "../../secret.txt") == ""


def test_read_file_truncates(tmp_path):
    cache = RepoCache(base_dir=tmp_path)
    path = _make_fake_clone(tmp_path, "lkmotto/sample")
    (path / "big.txt").write_text("a" * 10_000)
    out = cache.read_file("lkmotto/sample", "big.txt", max_bytes=100)
    assert "[truncated]" in out


def test_list_files_filters_suffix_and_skips_git(tmp_path):
    cache = RepoCache(base_dir=tmp_path)
    path = _make_fake_clone(tmp_path, "lkmotto/sample")
    (path / "a.py").write_text("x")
    (path / "b.md").write_text("y")
    (path / ".git" / "config").write_text("z")
    files = cache.list_files("lkmotto/sample", suffix=".py")
    assert "a.py" in files
    assert "b.md" not in files
    assert all(".git" not in f for f in files)


def test_recent_commits_no_clone_returns_empty(tmp_path):
    cache = RepoCache(base_dir=tmp_path)
    assert cache.recent_commits("lkmotto/missing") == []
