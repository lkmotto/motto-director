"""
LinkedIn recruiting MCP server (stub).

Exposes four tools to motto-director and any spawned Factory/Ona agent:

    - search_candidates(query, location, filters)
    - get_profile(linkedin_url)
    - send_connection_request(profile_url, message)
    - list_connections()

Auth model: email + password via the unofficial `linkedin-api`
(tomquirk) Python library. Credentials are loaded at boot from:

    LINKEDIN_EMAIL
    LINKEDIN_PASSWORD

These are populated by motto-director from Doppler. The companion
`motto-credential-grabber/setup_linkedin_creds.py` wizard is the
intended way to put them into Doppler in the first place.

Run as a stdio MCP server:
    python -m integrations.linkedin_mcp_server

Self-test (does not call LinkedIn, only verifies env vars are loaded):
    python -m integrations.linkedin_mcp_server --self-test
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

log = logging.getLogger("linkedin_mcp")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


LINKEDIN_EMAIL = os.environ.get("LINKEDIN_EMAIL")
LINKEDIN_PASSWORD = os.environ.get("LINKEDIN_PASSWORD")


def _require_creds() -> None:
    missing = [
        name for name, val in (
            ("LINKEDIN_EMAIL", LINKEDIN_EMAIL),
            ("LINKEDIN_PASSWORD", LINKEDIN_PASSWORD),
        ) if not val
    ]
    if missing:
        raise RuntimeError(
            f"LinkedIn MCP missing required env vars: {missing}. "
            "Run motto-credential-grabber/setup_linkedin_creds.py to populate Doppler."
        )


def _client():
    """
    Lazily import and construct a `linkedin-api` client using email/password.

    NOTE: `linkedin-api` is an unofficial library that scrapes LinkedIn's
    Voyager API. It violates LinkedIn's ToS and may trigger account
    challenges or restrictions. Use a dedicated burner account.
    """
    _require_creds()
    try:
        from linkedin_api import Linkedin
    except ImportError as exc:
        raise RuntimeError(
            "linkedin-api not installed. `pip install linkedin-api`."
        ) from exc
    return Linkedin(LINKEDIN_EMAIL, LINKEDIN_PASSWORD)


# ---------------------------------------------------------------------------
# Tool stubs
# ---------------------------------------------------------------------------

def search_candidates(
    query: str,
    location: str | None = None,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Search LinkedIn for candidates.

    Args:
        query: keywords to match against headline/title/skills,
            e.g. "senior backend engineer go".
        location: optional free-form location filter, e.g. "Berlin" or
            "Remote, Europe".
        filters: optional structured filters such as
            `current_company`, `past_companies`, `industries`,
            `schools`, `connection_of`, `network_depths`.

    Returns:
        A list of candidate dicts with `public_id`, `name`, `headline`,
        `location`, `current_company`, and `profile_url`.
    """
    raise NotImplementedError("search_candidates: stub, wire linkedin-api search_people()")


def get_profile(linkedin_url: str) -> dict[str, Any]:
    """
    Fetch a single LinkedIn profile.

    Args:
        linkedin_url: full URL (https://www.linkedin.com/in/<public_id>/)
            or bare `public_id`.

    Returns:
        Full profile dict (experience, education, skills, summary,
        contact info where visible).
    """
    raise NotImplementedError("get_profile: stub, wire linkedin-api get_profile()")


def send_connection_request(profile_url: str, message: str | None = None) -> dict[str, Any]:
    """
    Send a connection request to a LinkedIn profile.

    Args:
        profile_url: full URL or bare `public_id`.
        message: optional personalized note, max 300 chars per LinkedIn's
            invite limit.

    Returns:
        Dict with `status` (one of `sent`, `already_connected`,
        `limited`, `error`) and a human-readable `detail`.
    """
    raise NotImplementedError("send_connection_request: stub, wire linkedin-api add_connection()")


def list_connections() -> list[dict[str, Any]]:
    """
    List the authenticated user's first-degree connections.

    Returns:
        A list of connection dicts. The shape mirrors `linkedin-api`'s
        `get_profile_connections` output.
    """
    raise NotImplementedError("list_connections: stub, wire linkedin-api get_profile_connections()")


# ---------------------------------------------------------------------------
# MCP stdio loop
# ---------------------------------------------------------------------------

TOOLS = {
    "search_candidates": search_candidates,
    "get_profile": get_profile,
    "send_connection_request": send_connection_request,
    "list_connections": list_connections,
}


def _dispatch(line: str) -> dict[str, Any]:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid json: {exc}"}

    tool = msg.get("tool")
    args = msg.get("args", {}) or {}
    if tool not in TOOLS:
        return {"ok": False, "error": f"unknown tool: {tool}", "known": list(TOOLS)}
    try:
        return {"ok": True, "result": TOOLS[tool](**args)}
    except NotImplementedError as exc:
        return {"ok": False, "error": str(exc), "stub": True}
    except Exception as exc:  # noqa: BLE001
        log.exception("tool %s raised", tool)
        return {"ok": False, "error": str(exc)}


def _stdio_loop() -> None:
    log.info("linkedin_mcp_server stdio loop started")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        resp = _dispatch(line)
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


def _self_test() -> int:
    try:
        _require_creds()
    except RuntimeError as exc:
        log.error("self-test failed: %s", exc)
        return 1
    log.info(
        "self-test ok: LINKEDIN_EMAIL=%s (password set: %s)",
        LINKEDIN_EMAIL,
        bool(LINKEDIN_PASSWORD),
    )
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    _stdio_loop()
