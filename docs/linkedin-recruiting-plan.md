# LinkedIn Recruiting MCP Integration Plan

Owner: motto-director
Status: draft / design
Target: add a LinkedIn recruiting MCP server that the director and downstream
Factory/Ona agents can call as a first-class tool, for candidate sourcing,
profile enrichment, and outreach.

---

## 1. Goals

- Source candidates by query, location, and filters from LinkedIn.
- Enrich a single profile from its public URL.
- Send connection requests with a personalized note.
- List the operator's existing first-degree connections.
- Expose all of this as an MCP server so motto-director and any spawned
  Factory droid can consume it like any other Ona tool.

Non-goals (v1):
- Full InMail / Recruiter seat parity.
- Multi-account orchestration.
- Long-form messaging campaigns (handled later by a separate outreach agent).

---

## 2. Approach comparison

| Option | Cost | Risk | Coverage | Notes |
|---|---|---|---|---|
| `linkedin-api` (tomquirk, unofficial) + `li_at` cookie | Free | High (TOS / ban) | Search, profile, connect, messages | Best for low-volume, internal recruiting on a burner / operator account. Throttle aggressively. |
| ProxyCurl API | ~$49/mo for 1000 credits (Person Lookup ~1 credit) | Low | Profile enrichment, company lookup, search via Person Search API | No LinkedIn account needed; pulls from cached public data. Cannot send connection requests. |
| Phantombuster | $59-$259/mo | Medium | Search, scrape, connect, message via cloud browser agents | Good UX, but slower iteration and not MCP-native. |
| LinkedIn official API (Marketing / Talent Solutions) | Free tier exists, but Talent / Recruiter endpoints are partner-gated | Lowest | Limited; full Recruiter API requires partnership approval (months) | Not viable for an indie / fleet build in v1. |
| LinkedIn Recruiter (seat) | $180+/mo (Lite) to $835+/mo (Corporate) | Lowest | Full sourcing + InMail | Use only if a human recruiter is in the loop. |

### Recommendation

**Hybrid: `linkedin-api` for actions + ProxyCurl for read-only enrichment.**

- Use `linkedin-api` with a dedicated burner `li_at` / `JSESSIONID` cookie
  for: search, connection requests, listing connections, sending messages.
- Use ProxyCurl (optional, gated by `PROXYCURL_API_KEY` presence) for
  enrichment-heavy reads — it's safer per profile and avoids burning the
  cookie for what is effectively public data.
- If ProxyCurl key is absent, fall back to `linkedin-api`'s
  `get_profile()` for enrichment.

This gives us a free MVP path (just the cookie), and a graceful upgrade
path (add ProxyCurl when scale or safety matters), without changing the
MCP tool surface.

---

## 3. MCP tool surface

The MCP server exposes the following tools to motto-director and any
Factory droid:

### `search_candidates(query, location=None, filters=None, limit=25)`
- `query` (str, required): keywords, e.g. `"senior backend engineer go"`.
- `location` (str, optional): free-form location, e.g. `"Berlin"` or
  `"Remote, Europe"`.
- `filters` (dict, optional): structured filters — `current_company`,
  `past_companies`, `industries`, `schools`, `connection_of`,
  `network_depths` (`F`, `S`, `O`).
- `limit` (int, default 25, max 100).
- Returns: list of `{public_id, urn, name, headline, location,
  current_company, profile_url}`.

### `get_profile(linkedin_url)`
- `linkedin_url` (str, required): full URL or `public_id`.
- Returns: full profile dict — experience, education, skills, summary,
  contact info if visible, plus the source (`linkedin-api` or
  `proxycurl`).

### `send_connection_request(profile_url, message=None)`
- `profile_url` (str, required).
- `message` (str, optional, max 300 chars per LinkedIn limit).
- Returns: `{status: "sent" | "already_connected" | "limited" | "error",
  detail: str}`.
- Hard-caps at N invites / 24h (configurable via
  `LINKEDIN_DAILY_INVITE_CAP`, default 20) to stay under
  LinkedIn's weekly throttle (~100/wk before warnings).

### `list_connections(start=0, count=50)`
- Returns: paginated list of first-degree connections.

All tools return structured JSON. All tools log to ONA Fleet via
`record_event` so the director can perceive outreach activity.

---

## 4. Secrets / Doppler keys

Stored in Doppler project `motto-core`, config `prd`:

| Key | Required | Source | Notes |
|---|---|---|---|
| `LINKEDIN_LI_AT_COOKIE` | yes | Browser DevTools -> Application -> Cookies -> `li_at` on linkedin.com | Rotated by credential-grabber. |
| `LINKEDIN_JSESSIONID` | yes | Same cookie jar, `JSESSIONID` (strip quotes). | Used as CSRF token. |
| `LINKEDIN_USER_AGENT` | no | Pin a stable desktop UA string. | Defaults to a Chrome on macOS UA. |
| `LINKEDIN_DAILY_INVITE_CAP` | no | int | Default 20. |
| `PROXYCURL_API_KEY` | no | proxycurl.com dashboard | When present, profile reads route through ProxyCurl. |

The credential-grabber playbook for `linkedin` is a new entry in
`fleet.grabber_playbooks`:
- service: `linkedin`
- dashboard_url: `https://www.linkedin.com/`
- target_doppler_keys: `LINKEDIN_LI_AT_COOKIE`, `LINKEDIN_JSESSIONID`
- rotation cadence: weekly (cookies often last ~1y but recruiting flows
  invalidate them faster).

---

## 5. Architecture

```
                      +----------------------+
                      |   linkedin.com       |
                      |  (web session)       |
                      +----------+-----------+
                                 |
                  cookie capture | (browser session, manual or grabber)
                                 v
                      +----------------------+
                      | credential-grabber   |
                      |  playbook: linkedin  |
                      +----------+-----------+
                                 |
                  writes secrets | LINKEDIN_LI_AT_COOKIE
                                 | LINKEDIN_JSESSIONID
                                 v
                      +----------------------+
                      |       Doppler        |
                      |  motto-core / prd    |
                      +----------+-----------+
                                 |
                       env load  | (at boot)
                                 v
   +---------------------------------------------------------+
   |              motto-director MCP server                  |
   |                                                         |
   |   integrations/linkedin_mcp_server.py                   |
   |                                                         |
   |   tools: search_candidates                              |
   |          get_profile                                    |
   |          send_connection_request                        |
   |          list_connections                               |
   |                                                         |
   |   client choice:                                        |
   |     - linkedin-api (li_at cookie) -> actions + search   |
   |     - proxycurl   (api key)       -> safe enrichment    |
   +---------------------------+-----------------------------+
                               |
                  MCP protocol | (stdio / http)
                               v
   +---------------------------------------------------------+
   |         motto-director (perceive/ideate/act)            |
   |                                                         |
   |  + Factory droid sessions                               |
   |  + Ona fleet agents (SDR, sourcer, outreach)            |
   |                                                         |
   |  every call -> ona-mcp record_event /                   |
   |                ona-mcp record_artifact_content          |
   +---------------------------------------------------------+
```

---

## 6. Cost comparison (TL;DR)

| Tier | Monthly cost | What you get |
|---|---|---|
| Free path (`linkedin-api` + cookie) | $0 | Full action surface, ban risk, ~100 invites/wk soft cap. |
| ProxyCurl Starter | ~$49 | 1000 profile lookups, no LinkedIn account needed. Read-only. |
| ProxyCurl Growth | ~$299 | ~10k profile lookups. Still read-only. |
| LinkedIn Recruiter Lite | $180+ | UI-driven sourcing, 30 InMail/mo, no API. |
| LinkedIn Recruiter (Corporate) | $835+ | 150 InMail/mo, advanced filters, no API. |
| LinkedIn Talent Solutions API | partner-gated | Full programmatic access, but months to approve. |

v1 ships on **Tier 1 + optional Tier 2**, which is what the stub
server below assumes.

---

## 7. Implementation steps

1. Add `linkedin-api` and `requests` to `requirements.txt` (and
   optionally `python-linkedin-v2` / `httpx` if we want a typed
   client).
2. Add `integrations/linkedin_mcp_server.py` (stub committed in this
   PR) exposing the four MCP tools above.
3. Add a `linkedin` playbook row to `fleet.grabber_playbooks` and wire
   the grabber to write `LINKEDIN_LI_AT_COOKIE` / `LINKEDIN_JSESSIONID`
   into Doppler.
4. Register the MCP server in motto-director's MCP config so spawned
   droids see it.
5. Add a smoke test: `python -m integrations.linkedin_mcp_server
   --self-test` that runs `search_candidates("droid", limit=1)` and
   asserts a non-empty result.
6. Throttle + retry: wrap every call in a per-day rate limiter keyed
   on `LINKEDIN_DAILY_INVITE_CAP` and a per-call jittered sleep
   (3-7s) to look human.
7. Observability: every tool call records an `ona-mcp` event with
   `kind="linkedin.<tool_name>"` and the result count.

---

## 8. Risks and mitigations

- **Cookie invalidation / account restriction.** Use a dedicated
  burner account. Cap invites. Jitter timings. Rotate via grabber
  weekly.
- **LinkedIn HTML / API drift.** `linkedin-api` is unofficial and
  breaks periodically. Pin a known-good version in
  `requirements.txt` and keep ProxyCurl as a fallback for reads.
- **PII handling.** Never persist full profile bodies to public logs.
  Store via `record_artifact_content` with `send_blocking=true` so
  the director's output_critic reviews before any outreach is sent.
- **Cold outreach quality.** Connection-request messages must be
  reviewed by the output_critic lens before send (same pattern as
  cold_email artifacts).
