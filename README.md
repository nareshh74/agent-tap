# agent-tap

**Local multi-agent usage analytics + operational debugging for Claude Code, Codex, GitHub Copilot CLI, Hermes, Raven, OmniRoute, Aider and Cline.**

agent-tap tails the logs and databases your coding agents already write and correlates sessions, tool calls, skills, runtime errors, parent-to-child subagent traces and scheduled runs into one local SQLite file, with a live dashboard on `http://127.0.0.1:6007`. No cloud, no telemetry, no Docker, no SDK to install in your agents.

- **Usage analytics**: sessions, spans, tokens, tool and skill leaderboards per harness.
- **Operational debugging**: a normalized error taxonomy, parent-to-child trace trees, scheduled-task and cron monitoring, per-source health.
- **Local and private**: prompt text is **not** stored unless you opt in ([docs/privacy.md](docs/privacy.md)). Runtime dependencies are `platformdirs`, `fastapi` and `uvicorn`.
- **Extensible**: six built-in adapters, a declarative TOML adapter for anything else, and a Python entry-point plugin API.

## Get started

Python 3.11+ on Linux, macOS and Windows (including WSL2).

**Install** (pick one):

```bash
uvx agent-tap doctor                 # zero-install try-out
pipx install agent-tap               # or: pip install agent-tap
pipx install git+https://github.com/nareshh74/agent-tap   # straight from GitHub
```

> The PyPI release is still pending. Until it lands, use the `git+https://...` line (it works with `uvx --from git+https://github.com/nareshh74/agent-tap agent-tap doctor` as well).

**Step 1: choose what to monitor.**

```bash
agent-tap init
```

`init` (alias `setup`) auto-detects which harnesses have data on disk, then asks which ones to monitor, whether to override paths (custom OmniRoute directory, WSL2, adapter directory) and whether to store full prompt text (default: no). It also runs on the first `run`, `serve` or `service install` when no config exists. For scripts and CI use `--yes`, `--non-interactive` or `--harnesses claude-code,codex`; without a terminal it never prompts.

**Step 2: start the collector and the live dashboard.**

```bash
agent-tap run --watch                # collect continuously and serve the dashboard
agent-tap serve                      # dashboard only, http://127.0.0.1:6007
agent-tap service install            # background auto-start: Task Scheduler / launchd / systemd --user
```

**Step 3: look around from the terminal.**

```bash
agent-tap doctor                     # config, databases, which harnesses were found, source health
agent-tap digest --hours 24          # sessions, tokens, tools, skills, error classes, schedules
agent-tap query errors --json        # presets: harnesses, errors, tools, sessions; or any read-only SQL
```

## Operational debugging features

Usage analytics tell you what your agents did; these tell you why something broke.

- **Normalized runtime error taxonomy (`error.class`)** across session logs *and* daemon/process logs, so identical failures group together instead of appearing as free text. Examples emitted today:
  - Raven: `first_byte_timeout` (120 s LLM first-byte limit), `litellm_midstream_fallback_error`, `acp_prompt_timeout_600s`, `subagent_max_tool_rounds_15`, `scheduled_approval_timeout_10s`, `cron_nested_schedule_blocked`.
  - Hermes: `mcp_oauth_400_refresh_loop` (a 5-minute MCP OAuth refresh failure loop is collapsed into hourly buckets instead of thousands of rows), `hermes_tool_error`.
  - Copilot CLI: `acp_mcp_disposal_race`, `copilot_tool_error`.
  - Claude Code: guardrail blocks such as `claude_sleep_guardrail_blocked`, plus tool errors.
  - Codex: turn errors, `codex_responses_retry_warn`, upstream `401` classes.
  - OmniRoute: `omniroute_http_<status>` (401, 499, 5xx), `omniroute_circuit_degraded`, app errors.
- **Parent-to-child trace correlation across harnesses**: a Raven session links to its ACP child sessions and second-degree subagents (via ACP frame session ids, `parent_thread_id` on Codex, `fork-context-ref` on Claude Code). The dashboard's **Correlation explorer** shows the tree, and the in-page **trace drawer** opens any trace's spans with redacted tool inputs and outputs.
- **Scheduled task and cron monitoring**: Windows Task Scheduler, macOS `launchd` plus `crontab`, Linux `systemd` user timers plus `crontab`, and the native cron/heartbeat histories of Raven and Hermes, with last run, next run, exit codes and failures.
- **Per-source health**: every adapter stage keeps a watermark and a `source_health` row (last event, last error), surfaced in `agent-tap doctor` and the dashboard, so a silently broken collector is visible.
- **SQL on everything**: the same data is a plain SQLite file you can query with `agent-tap query` or any SQLite tool ([docs/schema.md](docs/schema.md)).

## Dashboard

- **1. Per-harness summary**: sessions, spans, tokens, errors per harness.
- **1B. Hermes profile breakdown**: per-profile activity (profiles are discovered, not hardcoded).
- **1C / 1D. Scheduled tasks and crons** and their recent runs.
- **2. Tools leaderboard** and **3. Skills leaderboard**.
- **4. Errors**: crashes, timeouts and normalized error classes.
- **5. Correlation explorer**: parent session to child session across harnesses.
- Every table is sortable; clicking a row opens the trace/spans drawer.

## How agent-tap differs from other tools (and how to use it with them)

| | Usage, tokens, cost | Runtime error taxonomy from process logs | Cross-harness parent-to-child correlation | Cron / scheduled-task monitoring | Local SQLite, no Docker or SDK | Add new agents without code |
|---|---|---|---|---|---|---|
| **agent-tap** | Sessions, tokens, tools, skills (no dollar pricing) | Yes | Yes | Yes | Yes | Yes (TOML + entry points) |
| CodeBurn | Strong: cost estimates, models, optimization advice | No | No | No | Yes | No |
| ccusage | Strong: token and cost reports from local logs | No | No | No | Yes | No |
| agentsview | Session browsing and search with analytics | Not its focus | Not its focus | No | Yes | Not its focus |
| Arize Phoenix / Langfuse / OpenLIT | Strong for apps you instrument | Via your own instrumentation | Via trace context you propagate | No | Typically a server or Docker plus an SDK or OTLP exporter | Instrumentation, not log adapters |

This comparison is a summary of each project's usual focus and may be out of date; check their docs. Corrections are welcome as issues.

**The USP:** agent-tap reads what your agents *already write to disk*, so you get correlated analytics and debugging across many different agent harnesses without instrumenting anything, running a server, or sending data off the machine.

**Use it together:**

- Keep **CodeBurn** or **ccusage** for dollar-cost estimates and savings advice; use agent-tap to find out *which* sessions failed, timed out or looped, and what they were doing.
- Keep **agentsview** for reading and searching transcripts; use agent-tap for fleet-level error classes, trace trees and schedules.
- Use **Phoenix / Langfuse / OpenLIT** for the LLM apps you build and deploy; use agent-tap for the coding agents you run on your own machine. The span table is Phoenix-compatible in shape, so exporting to those tools is straightforward SQL.

## FAQ

**How do I monitor and debug Claude Code, Codex and GitHub Copilot CLI locally?**
Install agent-tap, run `agent-tap init` to pick the harnesses, then `agent-tap run --watch`. Open `http://127.0.0.1:6007` for usage analytics and errors, or run `agent-tap digest --hours 24` in the terminal. Everything stays in a local SQLite file.

**How do I trace parent-to-child subagents across coding agents?**
agent-tap links a parent session to the child sessions it spawned (Raven to ACP children and nested subagents, Codex `parent_thread_id`, Claude Code `fork-context-ref`). Use the dashboard's Correlation explorer and trace drawer, or query the `spans` table by `child.session_id`.

**Is there an open-source complement to CodeBurn and ccusage that also helps with debugging?**
Yes: agent-tap. It does not price tokens in dollars; it adds a normalized error taxonomy, trace correlation across harnesses and cron monitoring. Run it alongside CodeBurn or ccusage.

**Does agent-tap send my prompts anywhere?**
No. It makes no network calls, and prompt/tool text is dropped by default. Opt in with `store_prompt_text = true` or `AGENT_TAP_STORE_PROMPTS=1`. Secrets are redacted on a best-effort basis ([docs/privacy.md](docs/privacy.md)).

**Which agents does agent-tap support, and can I add my own?**
Claude Code, Codex, GitHub Copilot CLI, Hermes, Raven and OmniRoute are built in; Aider and Cline ship as declarative TOML examples. Add another agent with a TOML file or a Python adapter ([docs/writing-an-adapter.md](docs/writing-an-adapter.md)).

**What does it need to run?**
Python 3.11+ and three small dependencies. No Docker, database server, API key or account.

## CLI reference

| Command | Purpose |
|---|---|
| `agent-tap init [--yes\|--non-interactive] [--harnesses h1,h2] [--store-prompts]` | Interactive setup (alias `setup`); also offered on first `run`/`serve`/`service install` |
| `agent-tap doctor` | Show config, paths, discovered adapters, source health |
| `agent-tap run [--once\|--watch]` | Collect spans once, or continuously with the dashboard (`daemon` is an alias for `run --watch`) |
| `agent-tap serve [--port 6007]` | Dashboard only |
| `agent-tap query <sql\|preset> [--json]` | Run read-only SQL or a named preset against the spans DB |
| `agent-tap digest [--hours 24] [--json]` | Correlated session and error digest |
| `agent-tap prune [--days 90]` | Delete traces older than the window |
| `agent-tap service install\|status\|uninstall\|render [--os windows\|macos\|linux]` | Manage the background service; `render` prints the unit without installing |
| `agent-tap adapters list\|validate <file.toml>` | List adapters / validate a declarative adapter |
| `agent-tap config show` | Print the effective configuration |

Global flags: `--config <file>`; per-command flags override everything below.

## Configuration

Precedence: **CLI flags > `AGENT_TAP_*` env vars > `config.toml` > OS defaults**.

`config.toml` lives in the user config dir (`~/.config/agent-tap/` on Linux, `~/Library/Application Support/agent-tap/` on macOS, `%LOCALAPPDATA%\agent-tap\` on Windows) or at `AGENT_TAP_CONFIG`.

```toml
spans_db = "/path/to/spans.db"
state_db = "/path/to/state.sqlite"
store_prompt_text = false     # AGENT_TAP_STORE_PROMPTS=1 to enable
host = "127.0.0.1"            # AGENT_TAP_HOST
port = 6007                   # AGENT_TAP_PORT
interval = 5.0                # poll seconds
lookback_hours = 48.0
retention_days = 90
project_name = "agent-analytics"
enabled_harnesses = ["claude-code", "codex"]   # omit to use all discovered (legacy key: adapters)
adapters_dir = "~/.config/agent-tap/adapters"  # declarative *.toml adapters
wsl_discovery = false         # AGENT_TAP_WSL=1: look for OmniRoute in WSL2 (Windows)

[paths]                        # per-adapter data-dir overrides (or AGENT_TAP_<NAME>_DIR)
codex = "/custom/.codex"
```

Env vars: `AGENT_TAP_CONFIG`, `AGENT_TAP_SPANS_DB`, `AGENT_TAP_STATE_DB`, `AGENT_TAP_STORE_PROMPTS`, `AGENT_TAP_HOST`, `AGENT_TAP_PORT`, `AGENT_TAP_WSL`, `AGENT_TAP_OMNIROUTE_DIR`.

### Auto-discovered locations

| Harness | Locations checked |
|---|---|
| Claude Code | `~/.claude/projects`, `$XDG_CONFIG_HOME/claude/projects` |
| Codex | `$CODEX_HOME` or `~/.codex` |
| Copilot CLI | `~/.copilot` |
| Hermes | `$HERMES_HOME`, `~/.hermes`, `%LOCALAPPDATA%\hermes`, `$XDG_DATA_HOME/hermes`; `state.db` and every `profiles/*` |
| Raven | `~/.raven` |
| OmniRoute | `AGENT_TAP_OMNIROUTE_DIR`, `~/.omniroute`; WSL2 volumes when `wsl_discovery` is on |

## Adapters

Six built-in Python adapters ship via the `agent_tap.harnesses` entry-point group; declarative TOML adapters (examples for Aider and Cline/Roo Code in `examples/adapters/`) cover simple JSONL/SQLite logs without code. See [docs/writing-an-adapter.md](docs/writing-an-adapter.md). Storage layout: [docs/schema.md](docs/schema.md).

## License

MIT
