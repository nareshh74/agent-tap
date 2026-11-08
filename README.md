# agent-tap

agent-tap is a local usage analytics and operational monitoring tool for running services and coding-agent harnesses. It reads the logs, databases and health endpoints that Claude Code, Codex, GitHub Copilot CLI, Hermes, Raven, OmniRoute, EverOS, SearXNG, FreeLLMAPI, Firecrawl, Aider, Cline and your own services already produce, and stores them in one SQLite file.

A dashboard at `http://127.0.0.1:6007` shows usage, errors, parent-to-child traces and scheduled runs. Nothing leaves your machine, and prompt text is off by default. In this document, a harness is a coding agent program such as Claude Code or Codex.

<p align="center">
  <a href="assets/agent-tap-demo.mp4"><img src="assets/agent-tap-demo.gif" alt="agent-tap demo: harness activity, running services, error classes, scheduled tasks and a parent-to-child trace drawer" width="800"></a>
</p>

[Watch the narrated walkthrough (MP4, 60 seconds)](assets/agent-tap-demo.mp4). The data shown is synthetic.

| Harness activity and running services | Scheduled tasks and the trace drawer |
|---|---|
| ![Dashboard overview with KPI cards, the Harness Activity table and the Running Services & Gateways table](assets/dashboard-overview.png) | ![Scheduled Tasks & Crons table with the parent-to-child trace drawer open](assets/trace-drawer-and-crons.png) |

## Get started

You need Python 3.11 or later on Linux, macOS or Windows (including WSL2).

The PyPI release is pending. Until it is live, install from GitHub:

```bash
pipx install git+https://github.com/nareshh74/agent-tap
uvx --from git+https://github.com/nareshh74/agent-tap agent-tap doctor
```

After the release, these commands work too:

```bash
pipx install agent-tap
uvx agent-tap doctor
```

If you use Claude Code, you can also install the bundled skill with `/plugin marketplace add nareshh74/agent-tap`.

First, choose what to monitor:

```bash
agent-tap init
```

`init` scans your machine for harnesses and known services, then asks which ones to enable. It pre-selects what it finds. It also asks whether to override any data path and whether to store prompt text (the default is no). The same questions appear on the first `run`, `serve` or `service install` when no config file exists and a terminal is attached. For scripts, pass `--yes`, `--non-interactive` or `--harnesses claude-code,codex`. Without a terminal, `init` never prompts.

Now start the collector and the dashboard:

```bash
agent-tap run --watch       # collect continuously and serve the dashboard
agent-tap serve             # dashboard only
agent-tap service install   # start at login: Task Scheduler, launchd or systemd --user
```

Open `http://127.0.0.1:6007`. Then check your setup from the terminal:

```bash
agent-tap doctor                 # config, databases, what was found, source health
agent-tap digest --hours 24      # sessions, tokens, tools, skills, errors, schedules
agent-tap query errors --json    # presets: harnesses, errors, tools, sessions, or any read-only SQL
```

## What agent-tap monitors

Every monitored target is a service with one of three kinds. The kind decides which dashboard sections show it.

| Kind | Examples | Where it appears |
|---|---|---|
| harness | `claude-code`, `codex`, `copilot-cli`, `hermes`, `raven`, `aider`, `cline` | Harness Activity (sessions, turns, tool calls, skill calls, parent-to-child correlation) and every shared section |
| gateway | `omniroute`, `freellmapi` | Running Services & Gateways and every shared section |
| service | `everos`, `searxng`, `firecrawl`, and any HTTP, Docker, process or log service you add | Running Services & Gateways and every shared section |

The shared sections are Tool Usage & Latency, Skill Invocations, Crashes, Timeouts & Operational Errors, Scheduled Tasks & Crons, and source health.

Running Services & Gateways lists each non-harness service with its status (healthy, degraded or down), endpoint or container, request and check counts, error count, p50 and p90 latency, and last seen time.

The built-in services use these defaults. You can override any of them in `config.toml`.

| Service | Health endpoint | Docker containers read for request logs |
|---|---|---|
| `everos` | `http://127.0.0.1:8000` | none |
| `searxng` | `http://127.0.0.1:8888` | `searxng` |
| `freellmapi` | `http://127.0.0.1:31415` | `freellmapi-freellmapi-1` |
| `firecrawl` | `http://127.0.0.1:3003` | `firecrawl-api-1`, plus the companion containers for status |

Each probe writes a span with `span_kind` `SERVICE` and the attributes `service.name`, `service.kind`, `service.status`, `service.endpoint`, `http.status_code`, `service.latency_ms` and `error.class` ([schema](docs/schema.md)).

### Add your own service

Run `agent-tap onboard` (alias `service-add`) with a name, a kind, and at least one source:

```bash
agent-tap onboard --name billing-api --kind service --health-url http://127.0.0.1:9000/health
agent-tap onboard --name my-proxy --kind gateway --docker-container my-proxy-1
agent-tap onboard --name worker --kind service --process-name worker.exe --log-path ~/logs/worker.log
```

The command appends a `[[services]]` entry to `config.toml`. For the full set of keys and for writing a Python adapter, see [docs/writing-a-service-adapter.md](docs/writing-a-service-adapter.md). To add a harness that writes JSONL or SQLite logs, see [docs/writing-an-adapter.md](docs/writing-an-adapter.md).

## Dashboard sections

- **Harness Activity** has one row per harness with sessions, traces, spans, turns, LLM calls, tool calls, skill calls, tokens, errors and latency.
- **Running Services & Gateways** has one row per non-harness service.
- **Scheduled Tasks & Crons** lists Windows Task Scheduler, macOS launchd and crontab, Linux systemd user timers and crontab, plus the native crons of Raven and Hermes.
- **Recent Cron & Heartbeat Runs** shows the latest runs with status and duration.
- **Tool Usage & Latency** and **Skill Invocations** rank tools and skills by use, with error rates.
- **Crashes, Timeouts & Operational Errors** groups failures by normalized error class.
- **Parent-to-Child Correlation Explorer** links a parent session to the child sessions it started.

Every table sorts when you click a column header. Selecting a row opens the trace drawer with the spans of that trace. The **Report Issue** button in the header opens a pre-filled GitHub issue and copies a diagnostic bundle (see [Report a problem](#report-a-problem)).

## Operational debugging features

Usage analytics tell you what your agents and services did. These features tell you why something broke.

- **Normalized error classes.** Each failure gets an `error.class`, so identical failures group together instead of appearing as free text. The adapters emit classes such as these today.
  - Raven: `first_byte_timeout` (the 120 second LLM first-byte limit), `litellm_midstream_fallback_error`, `acp_prompt_timeout_600s`, `subagent_max_tool_rounds_15`, `scheduled_approval_timeout_10s`, `cron_nested_schedule_blocked`.
  - Hermes: `mcp_oauth_400_refresh_loop` and `hermes_tool_error`. A five-minute OAuth refresh loop collapses into hourly buckets instead of thousands of rows.
  - Copilot CLI: `acp_mcp_disposal_race` and `copilot_tool_error`.
  - Claude Code: guardrail blocks such as `claude_sleep_guardrail_blocked`, and tool errors.
  - Codex: turn errors, `codex_responses_retry_warn` and upstream `401` classes.
  - OmniRoute: `omniroute_http_<status>` (for example 401, 499 and 5xx) and `omniroute_circuit_degraded`.
  - Services: each service adapter reports a down or degraded state with its own `error.class`, for example when a health endpoint times out.
- **Parent-to-child traces across harnesses.** A Raven session links to its ACP child sessions and to second-degree subagents through ACP frame session ids, Codex `parent_thread_id` and Claude Code `fork-context-ref`. The correlation explorer shows the tree, and the trace drawer shows each span with secrets redacted.
- **Scheduled task monitoring.** Each schedule shows its last run, next run, exit code and failures.
- **Source health.** Every adapter stage keeps a watermark and a `source_health` row with the last event and the last error. `agent-tap doctor` and the dashboard show them, so a collector that stopped working is visible.
- **SQL on everything.** The data is a plain SQLite file. Query it with `agent-tap query` or any SQLite tool.

## How agent-tap compares to other tools

| | Usage, tokens, cost | Error classes from process logs | Cross-harness parent-to-child traces | Schedule monitoring | Running services and gateways | Local SQLite, no Docker or SDK | New agent or service without code |
|---|---|---|---|---|---|---|---|
| agent-tap | Sessions, tokens, tools and skills. No dollar pricing | Yes | Yes | Yes | Yes | Yes | Yes (TOML and entry points) |
| CodeBurn | Cost estimates, models and optimization advice | No | No | No | No | Yes | No |
| ccusage | Token and cost reports from local logs | No | No | No | No | Yes | No |
| agentsview | Session browsing and search with analytics | Not its focus | Not its focus | No | No | Yes | Not its focus |
| Phoenix, Langfuse, OpenLIT | Strong for apps you instrument | Through your own instrumentation | Through trace context you propagate | No | Through your own instrumentation | Usually a server or Docker plus an SDK or OTLP exporter | Instrumentation, not log adapters |

The table summarizes what each project usually focuses on and may be out of date. Check each project's documentation, and open an issue if a row is wrong.

agent-tap reads what your agents and services already write, so you get one analytics and debugging view across many different programs without instrumenting them or sending data off the machine.

You can run it next to the other tools:

- Keep CodeBurn or ccusage for dollar estimates and savings advice. Use agent-tap to find which sessions failed, timed out or looped.
- Keep agentsview to read and search transcripts. Use agent-tap for error classes, trace trees and schedules.
- Use Phoenix, Langfuse or OpenLIT for the LLM applications you build and deploy. Use agent-tap for the agents and services on your own machine. The span tables have the same shape as Phoenix, so exporting is a SQL query.

## Report a problem

Open an issue with one of the forms in [.github/ISSUE_TEMPLATE](.github/ISSUE_TEMPLATE): a bug report, or a request to support a new service or harness.

Before you file a bug, generate the diagnostics:

```bash
agent-tap bug-report --service firecrawl --error-text "paste the error or traceback here"
agent-tap bug-report --error-file error.txt --output report.md
agent-tap bug-report --open     # opens a pre-filled GitHub issue
```

The report lists your operating system, Python version, agent-tap version, enabled services and harnesses, source health, and recent errors from the agent-tap log. Before it writes anything, it masks secrets with the same rules as span redaction and replaces your home directory with `~`. Read the report before you post it. The dashboard's Report Issue button produces the same bundle.

## FAQ

**How do I monitor and debug Claude Code, Codex and GitHub Copilot CLI locally?**
Install agent-tap, run `agent-tap init` to pick the harnesses, then run `agent-tap run --watch`. Open `http://127.0.0.1:6007` for usage and errors, or run `agent-tap digest --hours 24`. Everything stays in a local SQLite file.

**How do I trace parent-to-child subagents across coding agents?**
agent-tap links a parent session to the child sessions it started. Use the Parent-to-Child Correlation Explorer and the trace drawer, or query the `spans` table by `child.session_id`.

**How do I monitor local services such as SearXNG, Firecrawl, EverOS or FreeLLMAPI?**
Select them in `agent-tap init`. agent-tap probes each health endpoint and reads the Docker container logs. For any other service, run `agent-tap onboard`.

**Is there an open-source complement to CodeBurn and ccusage that also helps with debugging?**
Yes. agent-tap does not price tokens in dollars. It adds error classes, trace correlation across harnesses, service monitoring and schedule monitoring. Run it next to CodeBurn or ccusage.

**Does agent-tap send my prompts anywhere?**
No. It makes no network calls except the health probes you configure against your own services. Prompt and tool text is dropped by default. To store it, set `store_prompt_text = true` or `AGENT_TAP_STORE_PROMPTS=1`. Secret redaction is pattern based, so treat it as best effort ([docs/privacy.md](docs/privacy.md)).

**What does it need to run?**
Python 3.11 or later and three small dependencies. It needs no Docker, database server, API key or account. Docker is only needed to read the logs of services that run in Docker.

## CLI reference

| Command | Purpose |
|---|---|
| `agent-tap init [--yes\|--non-interactive] [--harnesses h1,h2] [--store-prompts]` | Choose harnesses and services. Alias `setup` |
| `agent-tap onboard --name NAME --kind service\|gateway\|harness [--health-url URL] [--docker-container NAME] [--log-path FILE] [--process-name NAME]` | Add a service to `config.toml`. Alias `service-add` |
| `agent-tap doctor [--json]` | Show config, paths, what was found and source health |
| `agent-tap run [--once\|--watch]` | Collect once, or continuously with the dashboard. `daemon` is an alias for `run --watch` |
| `agent-tap serve [--port 6007]` | Run the dashboard only |
| `agent-tap query <sql or preset> [--json]` | Run read-only SQL or a preset against the spans database |
| `agent-tap digest [--hours 24] [--json]` | Print a session and error digest |
| `agent-tap prune [--days 90]` | Delete traces older than the window |
| `agent-tap bug-report [--error-text TEXT] [--error-file FILE] [--service NAME] [--open] [--json] [--output FILE]` | Build a diagnostic bundle for a GitHub issue |
| `agent-tap service install\|status\|uninstall\|render [--os windows\|macos\|linux]` | Manage the background service. `render` prints the unit without installing it |
| `agent-tap adapters list\|validate <file.toml>` | List adapters or validate a declarative adapter |
| `agent-tap config show` | Print the effective configuration |

The global flag `--config <file>` selects a config file.

## Configuration

Settings come from four places. Each one overrides the ones after it: command-line flags, `AGENT_TAP_*` environment variables, `config.toml`, and operating system defaults.

`config.toml` lives in the user config directory (`~/.config/agent-tap/` on Linux, `~/Library/Application Support/agent-tap/` on macOS, `%LOCALAPPDATA%\agent-tap\` on Windows). Set `AGENT_TAP_CONFIG` to use another path.

```toml
spans_db = "/path/to/spans.db"
state_db = "/path/to/state.sqlite"
store_prompt_text = false        # AGENT_TAP_STORE_PROMPTS=1 to enable
host = "127.0.0.1"               # AGENT_TAP_HOST
port = 6007                      # AGENT_TAP_PORT
interval = 5.0                   # poll interval in seconds
lookback_hours = 48.0
retention_days = 90
project_name = "agent-analytics"
enabled_services = ["claude-code", "codex", "searxng"]   # omit to use everything found
adapters_dir = "~/.config/agent-tap/adapters"            # declarative harness adapters
wsl_discovery = false            # AGENT_TAP_WSL=1 looks for OmniRoute in WSL2 on Windows

[paths]                          # data directory overrides, or AGENT_TAP_<NAME>_DIR
codex = "/custom/.codex"

[[services]]                     # one entry per onboarded service
name = "billing-api"
kind = "service"                 # harness, gateway or service
health_url = "http://127.0.0.1:9000/health"
docker_container = "billing-api-1"
log_path = "~/logs/billing.log"
process_name = "billing"
```

The older key names `enabled_harnesses` and `adapters` still work as aliases for `enabled_services`.

Environment variables: `AGENT_TAP_CONFIG`, `AGENT_TAP_SPANS_DB`, `AGENT_TAP_STATE_DB`, `AGENT_TAP_STORE_PROMPTS`, `AGENT_TAP_HOST`, `AGENT_TAP_PORT`, `AGENT_TAP_WSL` and `AGENT_TAP_OMNIROUTE_DIR`.

### Where agent-tap looks for harness data

| Harness | Locations checked |
|---|---|
| Claude Code | `~/.claude/projects`, `$XDG_CONFIG_HOME/claude/projects` |
| Codex | `$CODEX_HOME` or `~/.codex` |
| Copilot CLI | `~/.copilot` |
| Hermes | `$HERMES_HOME`, `~/.hermes`, `%LOCALAPPDATA%\hermes`, `$XDG_DATA_HOME/hermes`. It reads `state.db` and every `profiles` subdirectory |
| Raven | `~/.raven` |
| OmniRoute | `AGENT_TAP_OMNIROUTE_DIR`, `~/.omniroute`, and WSL2 volumes when `wsl_discovery` is on |

## More documentation

- [Storage schema](docs/schema.md)
- [Privacy](docs/privacy.md)
- [Write a harness adapter](docs/writing-an-adapter.md)
- [Write a service adapter](docs/writing-a-service-adapter.md)
- [Contributing](CONTRIBUTING.md)

## License

MIT
