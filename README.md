# motto-director

Autonomous perceive-ideate-act orchestration loop for Luke Motto's AI agent system.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in .env with real keys
```

## Run

```bash
# single cycle (dry run)
python main.py --once

# continuous loop (default 120s interval)
python main.py
```

## Architecture

- **perceive.py** — gathers fleet state, recent events, open intents, active sessions, goals
- **ideate.py** — calls claude-haiku to decide which tasks to spawn
- **act.py** — spawns Factory droids, handles completions, logs to Fleet
- **fleet_client.py** — JSON-RPC client for ONA Fleet MCP server
- **factory_client.py** — REST client for Factory API
- **session_store.py** — persists active droid sessions to `active_sessions.json`
- **goals.py** — loads/saves `goals.json`
- **completion_handler.py** — checks idle sessions, harvests output, records artifacts
