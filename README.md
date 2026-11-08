# agent-tap

Local-first, near-real-time analytics for AI coding agents. agent-tap tails the logs and databases your agents already write (Claude Code, Codex, Copilot CLI, Hermes, Raven, OmniRoute, plus anything you describe in a small TOML file), correlates sessions, tool calls, skills, errors and scheduled runs into one local SQLite file, and serves a dashboard on `127.0.0.1:6007`.

No cloud, no telemetry, no heavy dependencies (`platformdirs`, `fastapi`, `uvicorn`, stdlib `sqlite3`/`tomllib`). Prompt text is **not** stored unless you opt in ([docs/privacy.md](docs/privacy.md)).

## How it relates to other tools

- **CodeBurn** focuses on token/cost accounting and optimization advice per provider.
- **agentsview** focuses on browsing and searching session transcripts.
- **agent-tap** focuses on *correlated operations data across harnesses*: spans linked parent to child, tool and skill leaderboards, normalized error classes, and scheduled tasks/crons, all queryable as SQL in a local database.

They are complementary; use whichever answers your question.

## Quickstart

Python 3.11+ on Linux, macOS, Windows (and WSL2).

```bash
# Not on PyPI yet: install straight from GitHub
pipx install git+https://github.com/nareshh74/agent-tap   # or: uvx --from git+https://github.com/nareshh74/agent-tap agent-tap doctor
agent-tap init                  # choose which harnesses to track, confirm paths, opt in/out of prompt text
agent-tap run --once            # collect once
agent-tap serve                 # dashboard on http://127.0.0.1:6007
agent-tap service install       # keep collector + dashboard running at login
```

`agent-tap doctor` shows which harnesses were discovered, where data is stored, and source health.

### Choosing harnesses (`agent-tap init`)

`agent-tap init` (alias `setup`) scans your machine for `claude-code`, `codex`, `copilot-cli`, `hermes`, `raven`, `omniroute` and the bundled declarative adapters `aider` and `cline`, then asks:

1. Which harnesses to enable (detected ones are pre-selected; press Enter to accept).
2. Whether to override data paths (custom OmniRoute directory, WSL2 scan on Windows, declarative adapter directory).
3. Whether to store full prompt/tool text locally (default: no).

The answers are saved to `config.toml` as `enabled_harnesses`, `[paths]` and `store_prompt_text`; an existing file is backed up to `config.toml.bak` and unrelated keys are kept. The same flow runs automatically on the first `agent-tap run`, `serve` or `service install` when no `config.toml` exists and you are at a terminal.

Scripts, CI and coding agents never hang: pass `--yes` or `--non-interactive` to accept defaults (detected harnesses, prompt text off) or `--harnesses claude-code,codex` to choose explicitly, e.g. `agent-tap service install --harnesses claude-code,codex`. Without a TTY no prompt is ever shown.

## Dashboard

- **1. Per-harness summary**: sessions, spans, tokens, errors per harness.
- **1B. Hermes profile breakdown**: per-profile activity (profiles are discovered, not hardcoded).
- **1C / 1D. Scheduled tasks and crons**: OS schedulers (Task Scheduler, launchd/cron, systemd timers/cron) plus agent-native crons, and their recent runs.
- **2. Tools leaderboard** and **3. Skills leaderboard**.
- **4. Errors**: crashes, timeouts and normalized error classes.
- **5. Correlation explorer**: parent session to child session across harnesses.
- All tables are sortable; clicking a row opens the in-page trace/spans drawer.

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
