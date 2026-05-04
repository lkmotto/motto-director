import os

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-gh-token")
    monkeypatch.setenv("NORTHFLANK_API_KEY", "test-nf-key")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_TOKEN", "test-cc-token")
    monkeypatch.setenv("DIRECTOR_DRY_RUN", "0")
    # LLM provider keys are deliberately *not* preset — tests that exercise
    # ideate set the keys they care about explicitly so the failover chain is
    # observable.
    yield
    for k in (
        "GITHUB_TOKEN",
        "NORTHFLANK_API_KEY",
        "NORTHFLANK_API_TOKEN",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "DEEPSEEK_MODEL",
        "GROQ_MODEL",
        "OPENROUTER_MODEL",
        "DIRECTOR_ANTHROPIC_MODEL",
        "CLAUDE_CODE_SESSION_TOKEN",
        "DIRECTOR_DRY_RUN",
        "LLM_PROVIDER",
    ):
        os.environ.pop(k, None)
