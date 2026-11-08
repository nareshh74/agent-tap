# Write a service adapter

This guide shows how to monitor a running service that is not a coding agent: an HTTP API, a Docker container, a background process, or a log file. It covers three methods. Pick the first one that fits.

A service has a kind. Use `service` for a normal application, `gateway` for a model router or proxy, and `harness` for a coding agent. The kind decides where the service appears in the dashboard. Services of kind `service` and `gateway` appear in Running Services & Gateways. Services of kind `harness` appear in Harness Activity.

## Method 1: run `agent-tap onboard`

If you know the health URL, the Docker container name, the log path or the process name, run:

```bash
agent-tap onboard --name billing-api --kind service --health-url http://127.0.0.1:9000/health
```

You can combine sources. This example probes a URL, reads the container logs and tracks the process:

```bash
agent-tap onboard --name my-proxy --kind gateway \
  --health-url http://127.0.0.1:4000/health \
  --docker-container my-proxy-1 \
  --process-name my-proxy
```

The command appends a `[[services]]` entry to your `config.toml` and adds the name to `enabled_services` if that list exists. Run `agent-tap doctor` to confirm that agent-tap found the service.

## Method 2: write a `[[services]]` entry by hand

Add one table per service to `config.toml`:

```toml
[[services]]
name = "billing-api"            # required, unique, becomes the harness attribute on spans
kind = "service"                # harness, gateway or service (default: service)
health_url = "http://127.0.0.1:9000/health"
docker_container = "billing-api-1"
log_path = "~/logs/billing.log"
process_name = "billing"
log_format = "text"             # how to read log_path and container logs
```

Each source you set adds one kind of evidence:

| Key | What agent-tap does |
|---|---|
| `health_url` | Sends an HTTP GET every poll. It records the status code and the latency, and marks the service healthy, degraded or down. |
| `docker_container` | Runs `docker inspect` for the container state and `docker logs --since` for new lines. Request lines become spans. |
| `log_path` | Reads new lines from the file, using a byte offset so each poll reads only new data. |
| `process_name` | Checks that a process with that name is running. |
| `log_format` | Selects the log line parser. Lines that no parser recognizes are counted as plain log lines. |

Set at least one source. A service with only `health_url` still produces status, latency and error spans.

Each probe writes a span with these properties:

- `span_kind` is `SERVICE`.
- `harness` and `service.name` hold the service name, and `service.kind` holds the kind.
- `service.status` is `healthy`, `degraded` or `down`.
- `service.endpoint`, `http.status_code` and `service.latency_ms` describe the probe.
- `error.class` is set when the service is degraded or down, for example `billing-api_down`.

A built-in service name such as `searxng` or `firecrawl` overrides the defaults of that built-in. Use this to change a port or a container name.

You can share an entry as a standalone file. Examples live in `examples/services/`. Check one with:

```bash
agent-tap adapters validate examples/services/generic-http.toml
```

## Method 3: write a Python adapter

Write Python when you need to parse a log format that `log_format` does not cover or call an API that is not plain HTTP. The adapter follows the protocol in `agent_tap.adapters.base`. A service adapter sets `kind` to `"service"` or `"gateway"`. The helpers live in `agent_tap.adapters.service`.

```python
from agent_tap.adapters.base import Context, PollResult
from agent_tap.emitter import build_span
from agent_tap.state import make_span_id, make_trace_id


class BillingAdapter:
    name = "billing-api"
    kind = "service"

    def discover(self, ctx: Context) -> bool:
        return True  # return False when the service is not on this machine

    def poll(self, ctx, state, store, cutoff_ts) -> PollResult:
        store.add(build_span(
            trace_id=make_trace_id(self.name, "probe"),
            span_id=make_span_id(self.name, "probe", cutoff_ts),
            name="service.probe",
            span_kind="SERVICE",
            attributes={
                "harness": self.name,
                "service.name": self.name,
                "service.kind": self.kind,
                "service.status": "healthy",
            },
        ))
        return {self.name: (1, 0)}  # stage name -> (spans, errors)
```

Register the class in your package so agent-tap finds it:

```toml
[project.entry-points."agent_tap.harnesses"]
billing-api = "my_pkg.adapter:BillingAdapter"
```

Follow these rules:

- Open foreign databases read-only and use `state` watermarks, so each poll reads only new data.
- Use `build_span`. It redacts secrets and drops prompt text unless the user opted in.
- Raise freely. agent-tap isolates a failing adapter and shows its last error in `agent-tap doctor`.
- Keep `name` stable. Spans and `enabled_services` refer to it.

For the harness-specific parts of the protocol, see [Write a harness adapter](writing-an-adapter.md). For the span attributes, see the [storage schema](schema.md).
