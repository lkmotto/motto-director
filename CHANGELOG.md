# Changelog

All notable changes to motto-director will be documented in this file.

## [0.1.0] — Unreleased

### Added
- Initial release of motto-director: self-aware autonomous orchestrator for the motto stack.
- Perceive→Ideate→Act loop with multi-provider LLM failover chain (DeepSeek, Groq, OpenRouter, Anthropic).
- GitHub integration: read PRs/issues, file issues, merge PRs, spawn Claude sessions.
- Northflank integration: cron-based scheduling, job status reads.
- Structured JSON observability logs to stdout.
- Fleet awareness via motto-mcp-server integration.
- Dry-run mode for safe local development and testing.
- Compound PR feature with rolling branch per repo.
