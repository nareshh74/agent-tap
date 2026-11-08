# Storage schema

agent-tap writes two SQLite files (WAL mode). Paths come from `spans_db` and `state_db` in the config.

## Spans database

The layout is compatible with the Arize Phoenix SQLite schema, so Phoenix-style tooling can read it, but nothing here requires Phoenix.

| Table | Purpose | Key columns |
|---|---|---|
| `projects` | One row per project (default `agent-analytics`, config `project_name`) | `id`, `name` (unique) |
| `project_sessions` | One row per agent session | `session_id` (unique), `project_id`, `start_time`, `end_time` |
| `traces` | One row per trace (a session maps to one deterministic trace id) | `trace_id` (unique), `project_rowid`, `project_session_rowid`, `start_time`, `end_time` |
| `spans` | One row per span | `span_id` (unique), `trace_rowid`, `parent_id`, `name`, `span_kind`, `start_time`, `end_time`, `attributes` (JSON), `events` (JSON), `status_code` (`OK`/`ERROR`/`UNSET`), `status_message`, `cumulative_error_count`, `llm_token_count_prompt`, `llm_token_count_completion` (+ `cumulative_*` variants) |

Timestamps are UTC text, `YYYY-MM-DD HH:MM:SS.ffffff`. `span_kind` is one of `LLM CHAIN TOOL AGENT RETRIEVER EMBEDDING RERANKER EVALUATOR GUARDRAIL UNKNOWN`. Inserts use `INSERT OR IGNORE` on `span_id`, so re-polling is idempotent.

`attributes` is a nested JSON object: dotted keys are un-flattened (`llm.token_count.prompt` becomes `{"llm": {"token_count": {"prompt": N}}}`), and `openinference.span.kind` is added.

### Attribute conventions

| Key | Meaning |
|---|---|
| `harness` | Source adapter (`raven`, `hermes`, `claude-code`, `codex`, `copilot-cli`, `omniroute`, or a declarative adapter name) |
| `session.id` | Agent session id; drives `project_sessions` |
| `agent.name`, `child.session_id` | Agent and linked child/sub-agent session |
| `llm.model_name`, `llm.provider` | Model and provider |
| `llm.token_count.prompt` / `.completion` / `.total` | Token usage |
| `tool.name`, `tool.id`, `tool.call_count`, `tool.failure_count`, `tool.duration_ms` | Tool calls |
| `error.class`, `error.count`, `http.status_code`, `log.level`, `log.file` | Errors and log-derived spans |
| `skill.names`, `skill.count` | Skill injection/read spans (`skill.inject`, `skill.read`) |
| `hermes.profile` | Hermes profile name |
| `raven.*`, `codex.*`, `copilot.*`, `omniroute.*` | Harness-specific extras |
| `correlation.confidence` | How confidently a session was linked to a parent |
| `input.value`, `output.value`, `tool.parameters` | Prompt/response text; only stored when `store_prompt_text` is on |

Common span names: `session.turn`, `llm.call`, `tool.call`, `user.message`, `subagent.run`, `skill.inject`, `skill.read`.

## State database

Private bookkeeping so polling is incremental: `file_watermarks` (per-file offsets), `db_watermarks` (per-source row ids), `error_buckets` (collapsed repeated errors), session links used to correlate parent/child sessions, and `source_health` (per-stage last event, counts, last error, shown by `doctor` and the dashboard). Safe to delete; the next run re-scans within `lookback_hours`.
