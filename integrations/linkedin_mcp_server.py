"""LinkedIn Recruiting MCP server.

Exposes recruiting tools to the Motto agent fleet via the MCP protocol (stdio
transport). Credentials are injected at runtime via `doppler run`:

    doppler run --project motto-director --config prd -- python integrations/linkedin_mcp_server.py

Required environment variables (set in Doppler):
    LINKEDIN_CLIENT_ID
    LINKEDIN_CLIENT_SECRET
    LINKEDIN_ACCESS_TOKEN
    LINKEDIN_REDIRECT_URI
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

mcp = FastMCP("linkedin-recruiting")

_BASE_URL = "https://api.linkedin.com/v2"


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("LINKEDIN_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError(
            "LINKEDIN_ACCESS_TOKEN is not set. Run integrations/setup_linkedin_oauth.py first."
        )
    return {
        "Authorization": f"Bearer {token}",
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_candidates(
    keywords: str,
    location: str = "",
    title: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search LinkedIn for candidate profiles.

    Requires LinkedIn Recruiter API access for full People Search.
    With standard API access this returns the authenticated user's own
    profile as a placeholder until Recruiter API is provisioned.

    Args:
        keywords: Search keywords (skills, technologies, etc.)
        location: Geographic filter, e.g. "San Francisco, CA"
        title: Job title filter, e.g. "Software Engineer"
        limit: Maximum number of results to return (default 10)

    Returns:
        List of candidate profile summaries.
    """
    # NOTE: Full People Search requires LinkedIn Recruiter API partnership.
    # The standard /v2/people endpoint only returns the authenticated member.
    # This stub demonstrates the intended interface; swap the endpoint once
    # Recruiter API access is granted.
    params: dict[str, Any] = {
        "q": "search",
        "keywords": keywords,
        "count": limit,
    }
    if location:
        params["location"] = location
    if title:
        params["title"] = title

    async with httpx.AsyncClient() as client:
        resp = client.get(
            f"{_BASE_URL}/people",
            headers=_auth_headers(),
            params=params,
            timeout=15,
        )
        # Recruiter API not yet provisioned — fall back to /v2/me for now
        if resp.status_code == 403:
            me = client.get(f"{_BASE_URL}/me", headers=_auth_headers(), timeout=15)
            me.raise_for_status()
            profile = me.json()
            first = profile.get("localizedFirstName", "")
            last = profile.get("localizedLastName", "")
            return [
                {
                    "urn": profile.get("id"),
                    "name": f"{first} {last}".strip(),
                    "headline": profile.get("localizedHeadline", ""),
                    "note": "Recruiter API required for full candidate search",
                }
            ]
        resp.raise_for_status()
        data = resp.json()
        elements = data.get("elements", [])
        return elements[:limit]


@mcp.tool()
async def post_job(
    title: str,
    description: str,
    location: str,
    employment_type: str = "FULL_TIME",
    organization_id: str = "",
) -> dict[str, Any]:
    """Post a job listing to LinkedIn.

    Requires `rw_organization_admin` OAuth scope and a LinkedIn Company Page.

    Args:
        title: Job title
        description: Full job description (plain text or basic HTML)
        location: Location string, e.g. "Remote" or "Austin, TX"
        employment_type: One of FULL_TIME, PART_TIME, CONTRACT, TEMPORARY, VOLUNTEER, OTHER
        organization_id: LinkedIn organization URN (e.g. "urn:li:organization:12345")

    Returns:
        Dict with job posting ID and URL.
    """
    if not organization_id:
        organization_id = os.environ.get("LINKEDIN_ORGANIZATION_URN", "")
    if not organization_id:
        raise ValueError(
            "organization_id is required. Set LINKEDIN_ORGANIZATION_URN in Doppler "
            "or pass it explicitly."
        )

    payload = {
        "externalJobPostingId": None,
        "title": title,
        "description": {"text": description},
        "employmentStatus": employment_type,
        "workplaceTypes": [],
        "location": location,
        "listedAt": None,
        "jobPostingOperationType": "CREATE",
        "integrationContext": organization_id,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_BASE_URL}/jobPostings",
            headers=_auth_headers(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        job_id = resp.headers.get("x-restli-id", resp.json().get("id", "unknown"))
        return {
            "job_id": job_id,
            "url": f"https://www.linkedin.com/jobs/view/{job_id}",
            "status": "posted",
        }


@mcp.tool()
async def get_profile(
    profile_url: str = "",
    person_urn: str = "",
) -> dict[str, Any]:
    """Fetch a LinkedIn member profile.

    Provide either a profile URL or a LinkedIn person URN. If neither is
    provided, returns the authenticated user's own profile.

    Args:
        profile_url: Public LinkedIn profile URL (optional)
        person_urn: LinkedIn person URN, e.g. "urn:li:person:ABC123" (optional)

    Returns:
        Profile fields: id, name, headline, summary, vanityName.
    """
    if person_urn:
        # Encode URN for URL path
        encoded = person_urn.replace(":", "%3A")
        url = f"{_BASE_URL}/people/({encoded})"
    else:
        # Default to authenticated user
        url = f"{_BASE_URL}/me"

    fields = "id,localizedFirstName,localizedLastName,localizedHeadline,localizedSummary,vanityName"

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers=_auth_headers(),
            params={"projection": f"({fields})"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        first = data.get("localizedFirstName", "")
        last = data.get("localizedLastName", "")
        vanity = data.get("vanityName", "")
        return {
            "urn": data.get("id"),
            "name": f"{first} {last}".strip(),
            "headline": data.get("localizedHeadline", ""),
            "summary": data.get("localizedSummary", ""),
            "vanity_name": vanity,
            "profile_url": profile_url or f"https://www.linkedin.com/in/{vanity}",
        }


@mcp.tool()
async def send_inmail(
    recipient_urn: str,
    subject: str,
    body: str,
) -> dict[str, Any]:
    """Send an InMail message to a LinkedIn member.

    Requires LinkedIn Recruiter API access and the `w_member_social` scope.

    Args:
        recipient_urn: LinkedIn person URN of the recipient
        subject: Message subject line
        body: Message body text

    Returns:
        Dict with message ID and delivery status.
    """
    payload = {
        "recipients": {
            "values": [{"messagingMember": {"miniProfile": {"objectUrn": recipient_urn}}}]
        },
        "subject": subject,
        "body": body,
        "messageType": "INMAIL",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_BASE_URL}/messages",
            headers=_auth_headers(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        msg_id = resp.headers.get("x-restli-id", resp.json().get("id", "unknown"))
        return {
            "message_id": msg_id,
            "status": "sent",
            "recipient_urn": recipient_urn,
        }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
