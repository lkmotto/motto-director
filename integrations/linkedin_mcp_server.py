"""
LinkedIn recruiting MCP server (stub).

Exposes four tools to motto-director and any spawned Factory/Ona agent:

    - search_candidates(query, location, filters, limit)
    - get_profile(linkedin_url)
    - send_connection_request(profile_url, message)
    - list_connections(start, count)

Credentials are pulled from environment variables that motto-director
populates from Doppler at boot:

    LINKEDIN_LI_AT_COOKIE      (required for actions and unofficial search)
    LINKEDIN_JSESSIONID        (required, CSRF token)
    LINKEDIN_USER_AGENT        (optional, pinned UA)
    LINKEDIN_DAILY_INVITE_CAP  (optional, int, default 20)
    PROXYCURL_API_KEY          (optional; enrichment routes to ProxyCurl)

Run as a stdio MCP server:
    python -m integrations.linkedin_mcp_server

Self-test:
    python -m integrations.linkedin_mcp_server --self-test
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("linkedin_mcp")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class Config:
    li_at: str | None = field(default_factory=lambda: os.environ.get("LINKEDIN_LI_AT_COOKIE"))
    jsessionid: str | None = field(default_factory=lambda: os.environ.get("LINKEDIN_JSESSIONID"))
    user_agent: str = field(default_factory=lambda: os.environ.get("LINKEDIN_USER_AGENT", DEFAULT_UA))
    proxycurl_key: str | None = field(default_factory=lambda: os.environ.get("PROXYCURL_API_KEY"))
    daily_invite_cap: int = field(
        default_factory=lambda: int(os.environ.get("LINKEDIN_DAILY_INVITE_CAP", "20"))
    )

    def require_session(self) -> None:
        missing = [k for k, v in (("LINKEDIN_LI_AT_COOKIE", self.li_at),
                                  ("LINKEDIN_JSESSIONID", self.jsessionid)) if not v]
        if missing:
            raise RuntimeError(f"LinkedIn MCP missing required env vars: {missing}")


CFG = Config()


# ---------------------------------------------------------------------------
# Throttle / rate limiter
# ---------------------------------------------------------------------------

_invite_log: list[datetime] = []


def _human_jitter(min_s: float = 3.0, max_s: float = 7.0) -> None:
    time.sleep(random.uniform(min_s, max_s))


def _under_invite_cap() -> bool:
    cutoff = datetime.now(timezone.utc).timestamp() - 86_400
    recent = [t for t in _invite_log if t.timestamp() >= cutoff]
    _invite_log[:] = recent
    return len(recent) < CFG.daily_invite_cap


def _record_invite() -> None:
    _invite_log.append(datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Client factories
# ---------------------------------------------------------------------------

def _linkedin_client():
    """
    Returns a tomquirk/linkedin-api client. Lazily imported so the rest of
    motto-director doesn't pay the import cost.
    """
    CFG.require_session()
    try:
        from linkedin_api import Linkedin
    except ImportError as exc:
        raise RuntimeError(
            "linkedin-api not installed. Add `linkedin-api` to requirements.txt."
        ) from exc

    cookies = {"li_at": CFG.li_at, "JSESSIONID": CFG.jsessionid}
    return Linkedin(
        username="",
        password="",
        cookies=cookies,
        user_agent=CFG.user_agent,
        refresh_cookies=False,
    )


def _proxycurl_get(linkedin_url: str) -> dict[str, Any]:
    import httpx

    if not CFG.proxycurl_key:
        raise RuntimeError("PROXYCURL_API_KEY not set")
    resp = httpx.get(
        "https://nubela.co/proxycurl/api/v2/linkedin",
        params={"url": linkedin_url, "use_cache": "if-present"},
        headers={"Authorization": f"Bearer {CFG.proxycurl_key}"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def search_candidates(
    query: str,
    location: str | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    client = _linkedin_client()
    filters = filters or {}

    kwargs: dict[str, Any] = {
        "keywords": query,
        "limit": limit,
    }
    if location:
        kwargs["regions"] = [location]
    for src, dst in (
        ("current_company", "current_company"),
        ("past_companies", "past_companies"),
        ("industries", "industries"),
        ("schools", "schools"),
        ("connection_of", "connection_of"),
        ("network_depths", "network_depths"),
    ):
        if src in filters:
            kwargs[dst] = filters[src]

    _human_jitter(1.5, 3.5)
    raw = client.search_people(**kwargs)

    out: list[dict[str, Any]] = []
    for r in raw:
        public_id = r.get("public_id") or r.get("publicIdentifier") or ""
        out.append({
            "public_id": public_id,
            "urn": r.get("urn_id") or r.get("urn"),
            "name": (r.get("name") or
                     f"{r.get('firstName', '')} {r.get('lastName', '')}".strip()),
            "headline": r.get("headline") or r.get("jobtitle"),
            "location": r.get("location"),
            "current_company": r.get("current_company") or r.get("jobtitle"),
            "profile_url": f"https://www.linkedin.com/in/{public_id}" if public_id else None,
        })
    return out


def get_profile(linkedin_url: str) -> dict[str, Any]:
    if CFG.proxycurl_key:
        data = _proxycurl_get(linkedin_url)
        data["_source"] = "proxycurl"
        return data

    client = _linkedin_client()
    public_id = linkedin_url.rstrip("/").split("/")[-1]
    _human_jitter()
    profile = client.get_profile(public_id=public_id)
    contact = {}
    try:
        contact = client.get_profile_contact_info(public_id=public_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("contact info fetch failed: %s", exc)
    return {**profile, "contact_info": contact, "_source": "linkedin-api"}


def send_connection_request(profile_url: str, message: str | None = None) -> dict[str, Any]:
    if message and len(message) > 300:
        return {"status": "error", "detail": "message exceeds 300 chars"}
    if not _under_invite_cap():
        return {
            "status": "limited",
            "detail": f"daily invite cap of {CFG.daily_invite_cap} reached",
        }

    client = _linkedin_client()
    public_id = profile_url.rstrip("/").split("/")[-1]
    _human_jitter(4.0, 9.0)
    try:
        urn = client.get_profile(public_id=public_id).get("profile_id")
        if not urn:
            return {"status": "error", "detail": "could not resolve profile urn"}
        ok = client.add_connection(profile_public_id=public_id, message=message or "")
        if ok is False:
            return {"status": "error", "detail": "add_connection returned False"}
        _record_invite()
        return {"status": "sent", "detail": f"invite sent to {public_id}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "detail": str(exc)}


def list_connections(start: int = 0, count: int = 50) -> list[dict[str, Any]]:
    count = max(1, min(count, 100))
    client = _linkedin_client()
    _human_jitter(1.0, 2.5)
    raw = client.get_profile_connections(client.get_user_profile().get("plainId", ""))
    return raw[start:start + count]


# ---------------------------------------------------------------------------
# MCP stdio loop (minimal JSON-RPC-ish dispatcher)
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
        return {"error": f"invalid json: {exc}"}

    tool = msg.get("tool")
    args = msg.get("args", {}) or {}
    if tool not in TOOLS:
        return {"error": f"unknown tool: {tool}", "known": list(TOOLS)}
    try:
        return {"ok": True, "result": TOOLS[tool](**args)}
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
        results = search_candidates("droid", limit=1)
    except Exception as exc:  # noqa: BLE001
        log.error("self-test failed: %s", exc)
        return 1
    log.info("self-test ok, got %d result(s)", len(results))
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    _stdio_loop()
