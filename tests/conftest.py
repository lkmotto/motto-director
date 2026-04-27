import os

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-gh-token")
    monkeypatch.setenv("NORTHFLANK_API_TOKEN", "test-nf-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_TOKEN", "test-cc-token")
    monkeypatch.setenv("DIRECTOR_DRY_RUN", "0")
    yield
    for k in (
        "GITHUB_TOKEN",
        "NORTHFLANK_API_TOKEN",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_SESSION_TOKEN",
        "DIRECTOR_DRY_RUN",
    ):
        os.environ.pop(k, None)
