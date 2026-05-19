# Ona Agent Context — Motto Stack

Operational reference for Ona agents working in this workspace. Read this at the start of every session.

---

## 1. Repos in the lkmotto org

Full list from `gh repo list lkmotto`. Key repos:

| Repo | Default Branch | Language | Purpose |
|------|---------------|----------|---------|
| `motto-director` | `main` | Python | Self-aware autonomous orchestrator. Runs as Northflank cron: perceives repo + queue state, ideates next moves with Claude Opus, spawns Claude Code sessions, files issues, drives labor utilization across the stack. |
| `motto-mcp-server` | `main` | Python | FastMCP fleet-coordination server backed by Neon Postgres. Exposes 35 tools: run ledger, agent heartbeats, cross-agent intents, artifact review, credential grabber, local task queue. Also serves a minimal HTML dashboard. |
| `factory-perplexity-mcp` | `master` | TypeScript | Cloudflare Worker MCP server wrapping the Factory.ai Droid Swarm API (`/api/v0`). Exposes spawn_droid, list_droids, get_droid, message_droid, interrupt_droid, respawn_with_context, spawn_swarm, spawn_and_watch. Auth: `HORIZON_API_KEY` (fmcp_ token). |
| `fleet-control` | `master` | — | Control plane for fleet automation. Houses mission specs (missions/MOT-*.md) and the GitHub Actions workflow that fans out `droid exec` across all repos via self-hosted runner on legion. |
| `motto-conductor` | `main` | Python | CONDUCTOR meta-agent. Builds, deploys, monitors, nurses, and self-improves other agents. |
| `motto-credential-grabber` | `main` | Python | Registry-driven credential health checker and auto-retrieval pipeline. Droids run `grabber/bootstrap.py` at session start to self-heal broken Doppler secrets. Zero-retention, ephemeral. |
| `motto-sdr-agent` | `main` | Python | Autonomous AI SDR: prospect research, Lavender email generation, CRM gatekeeper, Apollo sequences. |
| `motto-social-agent` | `main` | Python | Autonomous social media posting agent for LinkedIn, Instagram, Facebook. |
| `motto-video-agent` | `main` | Python | Autonomous short-form video creator: Kling 2.6 + ElevenLabs + FFmpeg. |
| `motto-distribution` | `main` | Python | Multi-platform content distribution engine: LinkedIn, X, Reddit, Beehiiv, Facebook Groups. |
| `motto-shortform` | `main` | Python | FORGE: programmatic short-form video production pipeline. |
| `motto-appraisal-pipeline` | `main` | Python | Core appraisal workflow automation pipeline. |
| `motto-appraisal-cockpit` | `main` | TypeScript | Web cockpit for manual order submission, automation-gate timeline, human-intervention controls. |
| `appraisalos-bidding` | `main` | TypeScript | Agentic bidding service for automated appraisal order bidding. |
| `rw-order-monitor` | `main` | Python | Monitors Gmail for Renters Warehouse appraisal work orders via ZeroClaw + OpenRouter. |
| `downtime-email-agent` | `main` | Python | DownTime weekend email digest — curated events sent via Resend every Friday. |
| `downtime-event-agent` | `main` | Python | DownTime event collection: Playwright-based fetchers for AllEvents.in, Facebook Events, Eventbrite. |
| `motto-fleet-burn-rate-tracker` | `main` | Python | Daily fleet cost aggregation and burn rate reporting. |
| `motto-fleet-provisioner` | `main` | Python | Self-serve credential provisioning lane for the fleet. |
| `motto-linkedin-ads` | `main` | Python | LinkedIn performance marketing agent: engagement scoring, TLA briefs, CAC tracking. |
| `motto-outreach` | `main` | Python | Autonomous Reddit + X outreach agent. |
| `motto-plaid-sync` | `main` | Python | Plaid API integration for bank account connection and transaction syncing. |
| `motto-sharpener` | `master` | Python | Tokenless self-improvement loop running on DigitalOcean with local Ollama. |
| `hostinger-mcp-server` | `main` | Dockerfile | Hostinger API MCP server for Perplexity Computer connector. |
| `motto-finance-tracker` | `master` | TypeScript | Finance tracking service. |
| `downtime-app` | `master` | TypeScript | DownTime app frontend. |
| `downtime-backend` | `master` | Python | DownTime backend API. |
| `downtime-dfw` | `master` | TypeScript | DownTime DFW regional variant. |
| `appraisalos-site` | `main` | HTML | AppraisalOS marketing site. |
| `motto-appraisal-site` | `main` | HTML | Motto Appraisal Service website. |

---

## 2. This repo's role

This workspace (`workspaces`) is the **Ona orchestration workspace** — a bare Ona devcontainer environment used as the control plane for the Motto agent fleet. It has no application code of its own. Its purpose is to provide a persistent, authenticated shell from which Ona agents can:

- Spawn and monitor Factory droids on `legion` via the Factory MCP
- Write run telemetry to the Motto Fleet Ledger via the motto-fleet MCP
- Operate on any repo in the lkmotto org via the GitHub MCP
- Serve as the canonical location for MCP wiring config (`.ona/mcp-config.json`) and agent context (`AGENTS.md`)

The workspace is authenticated to Doppler (`motto-core/prd`) via a personal token, to GitHub as `lkmotto`, and to Factory via `HORIZON_API_KEY`.

---

## 3. Compute targets

**Always pass `computerId` explicitly to `spawn_droid`.** The server defaults to the first active computer, which may be the exhausted e2b pool.

| Computer | ID | Provider | Status | Use |
|----------|----|----------|--------|-----|
| `legion` | `fc715237-e805-47f3-a590-0b2561fea3e0` | `byom` (Luke's local machine) | ✅ Active — default | **Always use this** |
| `fleet-droid-01` | `2d1aeaf9-37b1-4924-aa76-ff9ae926d1a0` | `e2b` (pool) | ⚠️ Active but prone to 503 daemon exhaustion | Avoid unless legion is down |

**Canonical spawn call:**
```python
spawn_droid(
    prompt="...",
    computerId="fc715237-e805-47f3-a590-0b2561fea3e0",  # legion
    autonomy="off",
)
```

---

## 4. Ledger conventions

Every unit of work wraps in the Motto Fleet Ledger (motto-fleet MCP / `ona-mcp-proxy.ljm32901.workers.dev`):

```
record_run_start(agent_name, kind, intent)  →  run_id
  │
  ├── record_event(agent_name, kind, run_id, payload)   [one per step]
  ├── spawn_droid / message_droid / get_droid           [Factory calls]
  ├── record_event(...)                                  [outcome]
  │
  ├── record_artifact_content(run_id, agent_name, kind, body)  →  artifact_id
  │
  └── record_run_end(run_id, status, summary)
```

**Canonical green-path example:** run `61e1be5a-a686-4eed-8cf5-8c7b28a9a376`
- agent: `factory-bridge`, kind: `factory_e2e_smoke_local`
- Spawned droid `064642a3` on legion, sent two stepwise messages, both responded, artifact `14` recorded, run closed `success`.
- Replayable via: `get_run("61e1be5a-a686-4eed-8cf5-8c7b28a9a376")`

**Parallelization pattern:** for N concurrent droids, open one parent run + N child events (one `droid_dispatched` event per droid), then one artifact per droid, then close the parent run.

---

## 5. Secrets

All tokens live in Doppler project `motto-core`, config `prd`. Fetch at runtime — never echo to chat or commit values.

| Secret | Doppler Key | Used for |
|--------|-------------|----------|
| Factory MCP auth | `HORIZON_API_KEY` | Bearer token for `factory-perplexity-mcp` Cloudflare Worker |
| Factory REST API | `FACTORY_API_KEY` | Direct calls to `api.factory.ai/api/v0` (used inside the worker) |
| Motto Fleet Ledger | `MOTTO_MCP_AUTH_TOKEN` | Bearer token for `ona-mcp-proxy` Cloudflare Worker |
| GitHub | `GITHUB_PAT` | `lkmotto` PAT — repo read/write, PR/issue ops |
| Doppler personal | `DOPPLER_PERSONAL_TOKEN` | Doppler CLI auth (already configured in this workspace) |

Fetch pattern: `doppler secrets get <KEY> --plain --project motto-core --config prd`

MCP header injection pattern: `"Authorization": "Bearer ${exec:doppler secrets get HORIZON_API_KEY --plain --project motto-core --config prd}"`

---

## 6. MCP schema gotchas

Discovered through live testing — do not guess, use these exactly:

### factory MCP (`factory-perplexity-mcp` worker)

| Tool | Correct param | Wrong / common mistake |
|------|--------------|----------------------|
| `get_droid` | `sessionId` (string UUID) | ~~`droid_id`~~ |
| `get_droid` | `messageLimit` (int, 1–500, default 200) | Omitting causes 400 if server cap differs |
| `spawn_droid` | `computerId` (UUID string) | No `local`/`fleet` enum — must pass actual UUID |
| `message_droid` | `sessionId`, `text` | — |
| `interrupt_droid` | `sessionId` | — |

### motto-fleet MCP (`ona-mcp-proxy` worker)

| Tool | Correct param | Wrong / common mistake |
|------|--------------|----------------------|
| `record_artifact_content` | `body` (string) | ~~`content`~~ (rejected); body must be a string, not a dict |
| `record_run_end` | `summary` (dict) | Passing a plain string causes pydantic validation error |
| `record_event` | `payload` (dict), `run_id` (string UUID) | — |

### Transport notes

- `factory` worker is **stateless** (Cloudflare Worker) — no session ID needed; each POST is independent after initialize.
- `motto-fleet` worker is **stateful** — initialize once, capture `Mcp-Session-Id` header, pass on all subsequent calls.
