# motto-director

Autonomous orchestrator for the motto fleet. Runs a continuous perceive→ideate→act loop, reading fleet state from ONA Fleet, deciding what coding tasks need to happen, and spawning Factory droids to execute them in parallel.

---

## Architecture

```
                    ┌─────────────────────────────────────────┐
                    │           motto-director                │
                    │                                         │
  ONA Fleet MCP ───►│  perceive() → ideate() → act()         │
  (fleet state,     │                                         │
   recent events,   │  goals.json  active_sessions.json       │
   open intents)    └──────────────────┬──────────────────────┘
                                       │ spawn_droid(prompt)
                                       ▼
                              Factory API  (/v1/sessions)
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                  │                  │
                    ▼                  ▼                  ▼
               Droid A            Droid B            Droid C
            (goal: SDR)      (goal: Pipeline)   (goal: Bidding)
                    │                  │                  │
                    └──────────────────┼──────────────────┘
                                       │ commit / PR / report
                                       ▼
                                    Repos
                    (motto-sdr-agent, motto-appraisal-pipeline, ...)
                                       │
                                       ▼
                              ONA Fleet (events,
                              artifacts, run records)
```

---

## How it works

### Perceive
On each cycle, the director calls ONA Fleet MCP to collect:
- **Agent status** — which fleet agents are alive and what they're doing
- **Recent events** — last 30 minutes of fleet activity (sessions spawned/completed, errors)
- **Open intents** — cross-agent nudges directed at `motto-director`

### Ideate
The director reads `goals.json` for the current active goals list. For each active goal it checks whether a Factory droid is already running for that goal (via `active_sessions.json`). If not, and there are open session slots (`MAX_PARALLEL_DROIDS`), it queues a spawn.

The prompt sent to each droid includes:
- Goal title and description
- Primary repo to work in
- Currently active fleet agents
- Recent fleet event summary

### Act
For each queued spawn, the director calls `POST /v1/sessions` on the Factory API. The session ID is recorded in `active_sessions.json` with metadata (goal ID, spawn timestamp).

On the next cycle, the director checks each tracked session: if idle/completed, it records the completion to ONA Fleet and frees the slot.

### Cycle log format
All output is newline-delimited JSON to stdout:
```json
{"ts": "2026-05-17T14:00:00+00:00", "event": "director.cycle.start"}
{"ts": "...", "event": "director.perceived", "agents": 3, "recent_events": 12, "intents": 0}
{"ts": "...", "event": "director.ideated", "goals": 7, "spawnable": 2, "will_spawn": 2}
{"ts": "...", "event": "director.spawned", "session_id": "sess_abc123", "goal_id": "g2"}
{"ts": "...", "event": "director.cycle.done", "spawned": 2, "sessions_active": 3}
{"ts": "...", "event": "director.sleeping", "seconds": 120}
```

Filter Northflank logs by `event` field. Key events:
- `director.cycle.start` / `director.cycle.done` — confirms cycle fired
- `director.cycle.error` — something failed, check `error` field
- `director.spawned` — a new droid was launched
- `director.sessions_pruned` — completed sessions were cleared

---

## Goals system

Goals live in `goals.json` at the repo root:

```json
{
  "goals": [
    {
      "id": "g1",
      "title": "Director Autonomy Loop",
      "description": "motto-director runs continuously, perceives fleet+github state, spawns parallel Factory droids, ingests completions, and reports everything to ONA Fleet",
      "repos": ["motto-director"],
      "priority": 1,
      "status": "in_progress"
    }
  ]
}
```

**Status values:**
- `active` — goal is live; director will spawn droids for it
- `in_progress` — currently being worked on; director will spawn droids for it
- `paused` — skip for now
- `done` — completed; excluded from ideation

**Adding a new goal:**
1. Edit `goals.json` and add an entry with a unique `id`, descriptive `title`, and clear `description`
2. Set `status` to `active`
3. List the target `repos` (used for context in the droid prompt)
4. The director picks it up on the next cycle

**To pause a goal** without deleting it, set `"status": "paused"`.

---

## Running locally

```bash
# Install dependencies
pip install -r requirements.txt

# Copy env vars
cp .env.example .env   # fill in MOTTO_MCP_AUTH_TOKEN, FACTORY_API_KEY, ANTHROPIC_API_KEY

# One cycle, live (spawns real droids)
python main.py --once

# Continuous loop (production mode, cycles every CYCLE_INTERVAL_SECONDS)
python main.py
```

**With Doppler:**
```bash
doppler run --project motto-core --config prd -- python main.py --once
```

### Safe testing
Use `scripts/test_cycle.py` to inspect a full cycle without making external calls:
```bash
python scripts/test_cycle.py
```

---

## Running the webhook

`completion_webhook.py` is a lightweight FastAPI app that receives Factory session completion callbacks. Run it alongside the main loop when you want push-based completion ingestion instead of polling:

```bash
uvicorn completion_webhook:app --host 0.0.0.0 --port 8080
```

The webhook accepts `POST /webhook/factory/completion` with payload `{"session_id": "...", "status": "..."}` and records the completion to ONA Fleet. Additional endpoints: `GET /health`, `GET /sessions/active`, `GET /sessions/all`.

In production, wire the Factory session callback URL to `https://<director-host>/webhook/factory/completion`.

---

## Deploying to Northflank

### Prerequisites
- Docker image pushed to GHCR: `ghcr.io/lkmotto/motto-director:latest`
- Northflank project `motto-agents` exists
- `MOTTO_MCP_AUTH_TOKEN`, `FACTORY_API_KEY`, `ANTHROPIC_API_KEY` available in Doppler (`motto-core/prd`)

### Steps

1. **Build and push the image** (GitHub Actions handles this on push to `main`; or manually):
   ```bash
   docker build -t ghcr.io/lkmotto/motto-director:latest .
   docker push ghcr.io/lkmotto/motto-director:latest
   ```

2. **Create the service in Northflank dashboard:**
   - Project: `motto-agents`
   - Service type: **Deployment** (not a cron job — it loops internally)
   - Image: `ghcr.io/lkmotto/motto-director:latest`
   - Plan: `nf-compute-50` (0.5 vCPU / 512 MB)
   - Replicas: **1** (singleton — do not scale)

3. **Set environment variables** (see table below). Reference `northflank.json` for the full list.

4. **Safe rollout — first deploy with dry run:**
   - Add env var `DIRECTOR_DRY_RUN=1`
   - Check logs for `director.ideated` to confirm goals are loading
   - Remove `DIRECTOR_DRY_RUN` to go live

5. **Verify:**
   ```bash
   # Watch logs in Northflank dashboard, or via CLI:
   northflank get logs --projectId motto-agents --serviceId motto-director --tail 50
   ```
   You should see `director.cycle.done` every ~120 seconds.

### Updating

Push to `main` → GitHub Actions builds new image → trigger a Northflank redeploy:
```bash
northflank trigger redeploy --projectId motto-agents --serviceId motto-director
```

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MOTTO_MCP_AUTH_TOKEN` | yes | — | Bearer token for ONA Fleet MCP. Must match `motto-mcp-server`. |
| `FACTORY_API_KEY` | yes | — | Factory.ai API key for spawning droids. |
| `ANTHROPIC_API_KEY` | yes | — | Anthropic API key (used by Factory droids). |
| `ONA_FLEET_URL` | yes | `https://p01--motto-mcp-server--hq2dk45g4bfc.code.run/mcp` | ONA Fleet MCP endpoint. |
| `FACTORY_COMPUTER_ID` | yes | `fc715237-e805-47f3-a590-0b2561fea3e0` | Factory computer ID for droid sessions. |
| `CYCLE_INTERVAL_SECONDS` | no | `120` | Seconds between perceive→ideate→act cycles. |
| `MAX_PARALLEL_DROIDS` | no | `5` | Maximum concurrent Factory droid sessions. |
| `FACTORY_MODEL` | no | `claude-sonnet-4-6` | Model used by spawned Factory droids. |
| `FACTORY_BASE_URL` | no | `https://api.factory.ai/v1` | Factory API base URL. |
| `DIRECTOR_DRY_RUN` | no | `0` | Set `1` to skip actual spawning (logs only). |
| `LOG_LEVEL` | no | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`). |

---

## ONA Fleet integration

Every cycle the director records to ONA Fleet (`motto-mcp-server`):

| Fleet operation | When |
|---|---|
| `heartbeat` | Start and end of every cycle with `status` and session counts |
| `record_run_start` | At cycle start; returns a `run_id` used by all subsequent events |
| `record_run_end` | At cycle end with status (`success` / `error`) and summary metrics |
| `record_event` `session.spawned` | Each time a Factory droid is launched |
| `record_event` `session.completed` | Each time a tracked session is found idle |
| `consume_open_intents` | Drain any cross-agent nudges directed at `motto-director` |

The director registers as agent `motto-director` (kind: `variable`). It appears in the ONA Fleet dashboard and its run history is queryable via `ona-mcp___list_runs`.

**Checking director health from the Fleet dashboard:**
- Last heartbeat: `ona-mcp___get_fleet_status` → find `motto-director` → `last_seen_at`
- Recent runs: `ona-mcp___list_runs` with `agent_name: motto-director`
- Events: `ona-mcp___get_recent_events` with `agent_name: motto-director`

---

## Factory integration

The director uses the Factory API to spawn autonomous coding sessions:

### Session spawning
`POST /v1/sessions` with:
- `prompt` — goal description + fleet context
- `computer_id` — `FACTORY_COMPUTER_ID` (the Droid's execution environment)
- `model` — `FACTORY_MODEL` (default: `claude-sonnet-4-6`)
- `autonomy` — `high` (droids work until done, then stop)

### Session tracking
Active sessions are persisted in `active_sessions.json`. Each entry:
```json
{
  "sess_abc123": {
    "goal_id": "g2",
    "goal_title": "AMC Bidding Coverage",
    "spawned_at": "2026-05-17T14:00:00+00:00"
  }
}
```

### Completion detection
On each cycle, the director polls `GET /v1/sessions/{id}` for each tracked session. When `status` is `idle`, `completed`, `done`, or `finished`, the session is marked complete and the slot is freed.

For push-based completion, deploy `completion_webhook.py` and configure Factory to call `POST /complete` on session end.

### Parallel droids (swarm)
`MAX_PARALLEL_DROIDS` caps how many sessions run at once. The director fills available slots each cycle. When all goals have running sessions, `will_spawn: 0` is logged and the director just polls for completions.
