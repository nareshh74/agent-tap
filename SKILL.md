---
name: agent-tap
description: Inspect what local AI coding agents (Claude Code, Codex, Copilot CLI, Hermes, Raven, OmniRoute, and others) have been doing: sessions, tool and skill usage, errors, crons, and parent/child correlation. Use when asked about agent activity, failures, slow or failing tools, or to open the agent-tap dashboard.
---

# agent-tap

agent-tap stores correlated agent spans in a local SQLite file and serves a dashboard on `http://127.0.0.1:6007`.

## Workflow

0. First time on a machine: `agent-tap init --non-interactive` (or `--harnesses claude-code,codex`) writes `config.toml` without prompting; plain `agent-tap init` asks the user interactively to pick harnesses, paths and prompt-text storage. Only run it if the user wants setup.
1. `agent-tap doctor` first. It lists discovered harnesses, DB paths and source health. If a source shows an error, report it rather than guessing.
2. Refresh data if stale: `agent-tap run --once`.
3. Summarize recent activity: `agent-tap digest --hours 24 --json` and read per-harness counts, top tools/skills, and error classes.
4. Drill down with read-only SQL: `agent-tap query "SELECT name, status_code, COUNT(*) FROM spans GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20" --json`. Tables: `projects`, `project_sessions`, `traces`, `spans` (attributes are JSON; use `json_extract(attributes,'$.harness')`). See `docs/schema.md`.
5. To show the dashboard: `agent-tap serve` (or `agent-tap service install`, which offers harness selection if no config exists; add `--harnesses` or `--non-interactive` when scripting) and give the user the URL.

## Interpreting results

- `status_code = 'ERROR'` plus `error.class` identifies failure types; repeated identical errors are bucketed.
- `session.id` joins spans to sessions; `child.session_id` links a parent agent to its sub-agent session.
- Token counts are under `llm.token_count.*`. Prompt text is absent unless the user enabled `store_prompt_text`.

## Safety

- Never print, quote or forward secrets, tokens or credential files, even if they appear in span text. Redaction is best effort.
- Queries are read-only; do not run `prune` or `service install/uninstall` without the user asking.
- Do not enable `store_prompt_text` on the user's behalf; `agent-tap init --non-interactive` leaves it off.
