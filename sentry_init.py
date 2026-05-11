"""Sentry error tracking initialisation for motto-director.

Wires ``sentry-sdk`` to the ``SENTRY_DSN`` env var, tags every event with
``agent`` and ``host`` so we can slice fleet errors by repo and deployment
target, and exposes a small ``capture_main_loop`` decorator that captures any
exception escaping the main loop before re-raising.

Initialised automatically on import, so a single ``import sentry_init`` at the
top of an entrypoint module is enough.

Environment:
    SENTRY_DSN                 - DSN; when unset, init is a no-op.
    DEPLOY_ENV                 - environment name, defaults to ``prd``.
    DEPLOY_HOST                - overrides the default host tag.
    SENTRY_TRACES_SAMPLE_RATE  - traces sample rate, defaults to ``0.1``.
    GIT_SHA / RELEASE_SHA      - explicit release SHA; otherwise read from git.
"""
from __future__ import annotations

import functools
import os
import subprocess
from collections.abc import Callable

import sentry_sdk

AGENT_NAME = "motto-director"
DEFAULT_HOST = "northflank"


def _git_sha() -> str:
    sha = os.getenv("GIT_SHA") or os.getenv("RELEASE_SHA")
    if sha:
        return sha
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def init_sentry(agent: str = AGENT_NAME, host: str | None = None) -> bool:
    """Initialise Sentry with environment, release and context tags.

    Safe to call repeatedly; returns ``False`` when ``SENTRY_DSN`` is missing.
    """
    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("DEPLOY_ENV", "prd"),
        release=_git_sha(),
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
    )
    sentry_sdk.set_tag("agent", agent)
    sentry_sdk.set_tag("host", host or os.getenv("DEPLOY_HOST", DEFAULT_HOST))
    return True


def capture_main_loop[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Decorator: capture any exception escaping the main loop, then re-raise."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - we re-raise after capturing
            sentry_sdk.capture_exception(e)
            raise

    return wrapper


# Auto-init so ``import sentry_init`` at an entrypoint is sufficient.
init_sentry()
