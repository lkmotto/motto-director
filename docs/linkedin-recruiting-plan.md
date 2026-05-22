# LinkedIn Recruiting MCP Integration

## Overview

This document covers the architecture for integrating LinkedIn's official OAuth 2.0 API into the Motto Director agent fleet for recruiting workflows: job posting, candidate search, and InMail outreach.

---

## OAuth 2.0 Authentication

LinkedIn uses the Authorization Code Flow (3-legged OAuth). All API calls require a Bearer token obtained via this flow.

**Authorization endpoint:** `https://www.linkedin.com/oauth/v2/authorization`  
**Token endpoint:** `https://www.linkedin.com/oauth/v2/accessToken`  
**Scopes required:**
- `r_liteprofile` — read basic profile data
- `r_emailaddress` — read email
- `w_member_social` — post on behalf of member
- `rw_organization_admin` — manage job postings (requires LinkedIn partner access)
- `r_1st_connections_size` — candidate network data

> **Note:** Full People Search and InMail APIs require LinkedIn's **Recruiter API** tier, which requires a partnership agreement. The standard Marketing/Jobs API is available to all developers.

---

## Doppler Secret Keys

| Key | Description |
|-----|-------------|
| `LINKEDIN_CLIENT_ID` | OAuth app client ID from LinkedIn Developer Portal |
| `LINKEDIN_CLIENT_SECRET` | OAuth app client secret |
| `LINKEDIN_REDIRECT_URI` | Callback URI registered in the LinkedIn app (e.g. `http://localhost:8080/callback`) |
| `LINKEDIN_ACCESS_TOKEN` | Long-lived access token after OAuth flow completes |
| `LINKEDIN_REFRESH_TOKEN` | Refresh token (if using refresh token rotation) |

Store all secrets under Doppler project `motto-director`, config `prd`.

---

## MCP Tool Definitions

The `linkedin_mcp_server.py` exposes these tools to the agent fleet:

### `search_candidates`
Search LinkedIn for candidate profiles matching a query.

**Input:**
```json
{
  "keywords": "string",
  "location": "string",
  "title": "string",
  "limit": "integer (default 10)"
}
```
**Returns:** List of candidate profile summaries (name, headline, location, profile URL).

---

### `post_job`
Post a job listing to LinkedIn on behalf of the organization.

**Input:**
```json
{
  "title": "string",
  "description": "string",
  "location": "string",
  "employment_type": "FULL_TIME | PART_TIME | CONTRACT",
  "organization_id": "string (LinkedIn org URN)"
}
```
**Returns:** Job posting ID and URL.

---

### `get_profile`
Fetch a LinkedIn member's public profile by URL or URN.

**Input:**
```json
{
  "profile_url": "string (optional)",
  "person_urn": "string (optional)"
}
```
**Returns:** Profile fields: name, headline, summary, experience, education.

---

### `send_inmail`
Send an InMail message to a LinkedIn member (requires Recruiter API access).

**Input:**
```json
{
  "recipient_urn": "string",
  "subject": "string",
  "body": "string"
}
```
**Returns:** Message ID and delivery status.

---

## API Endpoints

| Tool | LinkedIn API Endpoint |
|------|-----------------------|
| `search_candidates` | `GET https://api.linkedin.com/v2/people?q=search` |
| `post_job` | `POST https://api.linkedin.com/v2/jobPostings` |
| `get_profile` | `GET https://api.linkedin.com/v2/me` or `/v2/people/{urn}` |
| `send_inmail` | `POST https://api.linkedin.com/v2/messages` (Recruiter API) |

All requests use `Authorization: Bearer <LINKEDIN_ACCESS_TOKEN>` header.

---

## Cost Comparison: LinkedIn vs ZipRecruiter vs Indeed

| Platform | Job Post Cost | Candidate Search | InMail/Contact | API Access |
|----------|--------------|-----------------|----------------|------------|
| **LinkedIn** | $0–$500+/post (pay-per-click or flat) | Recruiter Lite ~$170/mo; Recruiter ~$825/mo | Included in Recruiter plans | Free (standard); Partner agreement for Recruiter API |
| **ZipRecruiter** | ~$249–$499/mo (unlimited posts) | Passive (they surface candidates) | Via platform only | Paid API; enterprise pricing |
| **Indeed** | Free (organic) + sponsored ~$5–$500/post | Resume search ~$120–$300/mo | InMail-equivalent via platform | Limited public API; sponsored jobs API via partnership |

**Recommendation:** LinkedIn provides the most structured API and the highest-quality candidate data for technical/professional roles. For Motto's recruiting volume, start with LinkedIn's standard Jobs API (free tier) and upgrade to Recruiter API if InMail automation is needed.

---

## Integration Architecture

```
motto-director agent
        │
        ▼
linkedin_mcp_server.py  (MCP server, stdio transport)
        │
        ├── doppler run -- python linkedin_mcp_server.py
        │         (injects LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET, LINKEDIN_ACCESS_TOKEN)
        │
        └── LinkedIn API v2  (https://api.linkedin.com/v2/*)
```

The MCP server runs as a subprocess via `doppler run`, ensuring credentials are never written to disk. The agent calls tools over stdio using the `mcp` Python SDK.

---

## Setup Steps

1. Create a LinkedIn Developer app at https://developer.linkedin.com/
2. Add OAuth 2.0 redirect URI (e.g. `http://localhost:8080/callback`)
3. Note `Client ID` and `Client Secret`
4. Run `integrations/setup_linkedin_oauth.py` to complete the OAuth flow and store the access token
5. Store `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_REDIRECT_URI` in Doppler
6. Run the MCP server via `doppler run -- python integrations/linkedin_mcp_server.py`
