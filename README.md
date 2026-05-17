# motto-director

Intent-driven orchestrator for the motto fleet. The director only runs work when a human (or another agent) signals an intent to `motto-director` in ONA Fleet.

---

## Architecture

```
Human / source agent
        | signal_intent(target_agent="motto-director", payload={task,repo,specs})
        v
  ONA Fleet MCP (intent queue)
        |
        v
+---------------------------------------------------------------+
|                         motto-director                        |
|                                                               |
| perceive()  -> consume_open_intents(limit=10)                |
| ideate(intent) -> concrete droid prompt (Claude/DeepSeek)    |
| act() -> spawn Factory session + track attempts              |
| completion_handler -> success/failure parse + retry loop     |
|                                                               |
| active_sessions.json + goals.example.json (self-directed only)|
+---------------------------------------------------------------+
                                |
                                v
                      Factory API (/v1/sessions)
                                |
                                v
                        Droid execution sessions
                                |
                                v
                      ONA Fleet runs/events/artifacts
```

---

## How to trigger a build

Use `signal_intent` to enqueue work for `motto-director`.

Example intent:

```json
{
  "target_agent": "motto-director",
  "kind": "build_request",
  "source_agent": "motto-cockpit",
  "payload": {
    "task": "build ReadyWrite order monitoring service",
    "repo": "lkmotto/readywrite-monitor",
    "specs": "ingest webhook payloads, persist order state, expose health endpoint"
  }
}
```

Director behavior:
1. `perceive()` consumes open intents for `motto-director`
2. each consumed intent is decomposed into a concrete droid prompt
3. Factory session is spawned and tracked with attached intent metadata
4. completion is parsed for success/failure signals

If no intents are pending, director logs `No pending intents. Exiting.` and exits 0.

---

## Retry loop

When a session completes, the final assistant output is scanned for keyword signals:
- success keywords: `DONE,SUCCESS,COMPLETE,tests pass,exit 0`
- failure keywords: `ERROR,FAILED,blocked,exception,cannot proceed`

On failure:
- if retries remain (`MAX_RETRIES`, default `3`), a new droid is spawned with:
  - original prompt
  - failure context
  - `Previous attempt failed with: <error>. Fix and continue.`
- if max retries are exhausted, run closes as `error` and a failure summary artifact is recorded.

On success:
- run closes as `success`
- director signals the original source agent with completion payload.

---

## Self-directed fallback (disabled by default)

`goals.example.json` is preserved for a future autonudge phase.
It is only used when `--self-directed` is explicitly passed and no intents are present.

---

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env

# intent-driven one-shot
python main.py --once

# continuous polling mode
python main.py

# hidden fallback mode (self-directed)
python main.py --self-directed
```

---

## Running the webhook

```bash
uvicorn completion_webhook:app --host 0.0.0.0 --port 8080
```

Endpoint: `POST /webhook/factory/completion` with `{"session_id": "...", "status": "..."}`.

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MOTTO_MCP_AUTH_TOKEN` | yes | - | Bearer token for ONA Fleet MCP |
| `FACTORY_API_KEY` | yes | - | Factory API key |
| `ANTHROPIC_API_KEY` | yes | - | Anthropic key for ideation |
| `ONA_FLEET_URL` | yes | set in `northflank.json` | ONA Fleet MCP endpoint |
| `CYCLE_INTERVAL_SECONDS` | no | `120` | Polling interval in continuous mode |
| `MAX_PARALLEL_DROIDS` | no | `5` | Maximum concurrent sessions |
| `MAX_RETRIES` | no | `3` | Retry limit per intent |
| `SUCCESS_KEYWORDS` | no | `DONE,SUCCESS,COMPLETE,tests pass,exit 0` | Success output markers |
| `FAILURE_KEYWORDS` | no | `ERROR,FAILED,blocked,exception,cannot proceed` | Failure output markers |
| `FACTORY_COMPUTER_ID` | no | set in config | Factory computer ID |
| `FACTORY_MODEL` | no | `claude-sonnet-4-6` | Model for spawned sessions |
| `FACTORY_API_BASE` | no | `https://api.factory.ai/v1` | Factory API base URL |
| `LOG_LEVEL` | no | `INFO` | Python logging level |
