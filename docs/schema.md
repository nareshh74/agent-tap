# Storage schema

agent-tap writes two SQLite files in WAL mode. The `spans_db` and `state_db` settings give their paths.

## Spans database

The layout matches the Arize Phoenix SQLite schema, so Phoenix-style tools can read it. agent-tap does not need Phoenix.

| Table | Purpose | Key columns |
|---|---|---|
| `projects` | One row per project. The default is `agent-analytics`, set by `project_name` | `id`, `name` (unique) |
| `project_sessions` | One row per agent session | `session_id` (unique), `project_id`, `start_time`, `end_time` |
| `traces` | One row per trace. A session maps to one deterministic trace id | `trace_id` (unique), `project_rowid`, `project_session_rowid`, `start_time`, `end_time` |
| `spans` | One row per span | `span_id` (unique), `trace_rowid`, `parent_id`, `name`, `span_kind`, `start_time`, `end_time`, `attributes` (JSON), `events` (JSON), `status_code` (`OK`, `ERROR` or `UNSET`), `status_message`, `cumulative_error_count`, `llm_token_count_prompt`, `llm_token_count_completion` and the `cumulative_*` variants |

Timestamps are UTC text in the form `YYYY-MM-DD HH:MM:SS.ffffff`. The `span_kind` column holds one of `LLM`, `CHAIN`, `TOOL`, `AGENT`, `RETRIEVER`, `EMBEDDING`, `RERANKER`, `EVALUATOR`, `GUARDRAIL`, `SERVICE` or `UNKNOWN`. Inserts use `INSERT OR IGNORE` on `span_id`, so polling the same data twice adds no rows.

The `attributes` column is a nested JSON object. agent-tap un-flattens dotted keys, so `llm.token_count.prompt` becomes `{"llm": {"token_count": {"prompt": N}}}`, and it adds `openinference.span.kind`. Use `json_extract(attributes, '$.llm.token_count.prompt')` to read a value.

### Attribute conventions

| Key | Meaning |
|---|---|
| `harness` | Name of the source adapter or service, for example `raven`, `hermes`, `claude-code`, `codex`, `copilot-cli`, `omniroute`, `searxng` or a name you defined |
| `service.kind` | `harness`, `gateway` or `service`. agent-tap sets it from the adapter, and the dashboard uses it to choose the section |
| `service.name` | Name of the service on service probe spans |
| `service.status` | `healthy`, `degraded` or `down` on service probe spans |
| `service.endpoint` | Health URL, container or log path that the probe used |
| `service.latency_ms` | Probe latency in milliseconds |
| `session.id` | Agent session id. It fills `project_sessions` |
| `agent.name`, `child.session_id` | The agent and its linked child session |
| `llm.model_name`, `llm.provider` | Model and provider |
| `llm.token_count.prompt`, `.completion`, `.total` | Token usage |
| `tool.name`, `tool.id`, `tool.call_count`, `tool.failure_count`, `tool.duration_ms` | Tool calls |
| `error.class`, `error.count`, `http.status_code`, `log.level`, `log.file` | Errors and spans built from logs |
| `skill.names`, `skill.count` | Skill injection and read spans (`skill.inject`, `skill.read`) |
| `hermes.profile` | Hermes profile name |
| `raven.*`, `codex.*`, `copilot.*`, `omniroute.*` | Extra attributes of one harness |
| `correlation.confidence` | How confidently agent-tap linked a session to its parent |
| `input.value`, `output.value`, `tool.parameters` | Prompt and response text, stored only when `store_prompt_text` is on |

Common span names are `session.turn`, `llm.call`, `tool.call`, `user.message`, `subagent.run`, `skill.inject` and `skill.read`.

### Service spans

Each poll of a service writes one span with `span_kind` set to `SERVICE`. The span carries `service.name`, `service.kind`, `service.status`, `service.endpoint`, `http.status_code` and `service.latency_ms`. When the status is `degraded` or `down`, the span has `status_code` `ERROR` and an `error.class` such as `searxng_down`. Services that expose request logs, such as a Docker container, also write one span per parsed request with `http.status_code` and the request path.

## State database

The state database makes polling incremental. It holds `file_watermarks` (offsets per file), `db_watermarks` (row ids per source), `error_buckets` (repeated errors collapsed into one row), the session links that connect parent and child sessions, and `source_health` (the last event, counts and last error of each stage). `agent-tap doctor` and the dashboard show source health. You can delete the state database. The next run scans again within `lookback_hours`.
