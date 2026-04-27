import os

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-gh-token")
    monkeypatch.setenv("NORTHFLANK_API_KEY", "test-nf-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_TOKEN", "test-cc-token")
    monkeypatch.setenv("DIRECTOR_DRY_RUN", "0")
    yield
    for k in (
        "GITHUB_TOKEN",
        "NORTHFLANK_API_KEY",
        "NORTHFLANK_API_TOKEN",
        "ANTHROPIC_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "CLAUDE_CODE_SESSION_TOKEN",
        "DIRECTOR_DRY_RUN",
        "LLM_PROVIDER",
    ):
        os.environ.pop(k, None)
