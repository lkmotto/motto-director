# Ona Session Context

Full agent operational spec is in [`AGENTS.md`](../AGENTS.md) at the repo root. Read it first.

## Active MCP servers (`.ona/mcp-config.json`)

| Server key | Name | URL | Auth secret | Tools |
|------------|------|-----|-------------|-------|
| `factory` | Factory Droid Swarm | `factory-perplexity-mcp.ljm32901.workers.dev/mcp` | `HORIZON_API_KEY` | `list_computers`, `spawn_droid`, `spawn_swarm`, `spawn_and_watch`, `list_droids`, `get_all_active_droids`, `get_droid`, `get_droid_messages_full`, `message_droid`, `respawn_with_context`, `interrupt_droid` |
| `motto-fleet` | Motto Fleet Ledger | `ona-mcp-proxy.ljm32901.workers.dev/mcp` | `MOTTO_MCP_AUTH_TOKEN` | `record_run_start`, `record_run_end`, `record_event`, `record_artifact_content`, `get_run`, `list_runs`, `get_fleet_status`, `get_recent_events`, `register_agent`, `heartbeat`, `signal_intent`, `consume_open_intents` + 23 more |
| `github` | GitHub | stdio via Docker (`ghcr.io/github/github-mcp-server`) | `GITHUB_PAT` | `get_file_contents`, `push_files`, `create_issue`, `create_pull_request`, `list_commits`, `list_issues`, `search_repositories`, `search_code`, `get_pull_request`, `merge_pull_request` + 16 more |

## Quick reference

- **Default compute:** `legion` — `computerId: fc715237-e805-47f3-a590-0b2561fea3e0`
- **Doppler project:** `motto-core/prd`
- **Canonical green run:** `61e1be5a-a686-4eed-8cf5-8c7b28a9a376`
- **GitHub org:** `lkmotto`
