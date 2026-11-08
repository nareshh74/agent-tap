# Write a harness adapter

An adapter turns the logs or databases of one program into spans. This guide covers harnesses, which are coding agents. For services such as HTTP APIs and Docker containers, see [Write a service adapter](writing-a-service-adapter.md).

You can add a harness in two ways. Use a declarative TOML file when the harness writes JSONL, JSON or SQLite data. Use a Python adapter when you need custom parsing.

## Method 1: declarative TOML

Put a `.toml` file in the adapters directory. The `adapters_dir` setting names it, and the default is `adapters/` inside the user config directory. To check a file before you install it, run:

```bash
agent-tap adapters validate examples/adapters/aider.toml
agent-tap adapters list
```

Two examples ship with agent-tap. `examples/adapters/aider.toml` reads JSONL. `examples/adapters/cline.toml` reads a JSON array, and it works for Roo Code if you change the extension id in the glob.

### Schema

```toml
name = "my-harness"        # required, becomes the harness attribute and the span name prefix
kind = "jsonl"             # required: jsonl, json_array or sqlite

[source]
# for jsonl and json_array
paths = ["~/.my-harness/*.jsonl"]   # globs, with ~ and environment variables expanded
# for sqlite
db = "~/.my-harness/state.db"
query = "SELECT rowid AS id, * FROM events WHERE rowid > :last_rowid"  # must use :last_rowid, and :cutoff (epoch seconds) is also bound
rowid_column = "id"        # column used as the incremental watermark, default "rowid"
table = "events"           # label used for the watermark

[map]                      # each value is a dotted path into a record, for example "a.b.0.c"
timestamp = "time"         # required, epoch seconds, milliseconds or nanoseconds, or ISO 8601
session_id = "user_id"     # required
name = "event"             # span name, prefixed with the adapter name
id = "uuid"                # source of the stable span id
end_timestamp = "ended"
span_kind = "kind"         # path to a kind value, or set span_kind_default = "CHAIN"
input = "prompt"           # stored only when store_prompt_text is on
output = "text"
model = "model"
tokens_prompt = "usage.in"
tokens_completion = "usage.out"
error = "error"            # a truthy value marks the span as an error

[where]                    # optional, keeps only records where the path equals the value
type = "say"
```

For file sources, the pseudo-fields `_file` (the file name without extension) and `_dir` (the parent directory name) are available in `[map]`. They work well as a session id. agent-tap reads JSONL files by byte offset, JSON arrays by item count and SQLite by `:last_rowid`. It skips invalid files, and `adapters validate` reports them.

## Method 2: Python adapter

Implement the protocol from `agent_tap.adapters.base`:

```python
from pathlib import Path

from agent_tap.adapters.base import Context, PollResult
from agent_tap.emitter import build_span
from agent_tap.state import make_span_id, make_trace_id


class MyAdapter:
    name = "my-harness"
    kind = "harness"

    def discover(self, ctx: Context) -> bool:
        return (ctx.dir_for(self.name) or Path.home() / ".my-harness").exists()

    def poll(self, ctx, state, store, cutoff_ts) -> PollResult:
        # state is the StateStore with file and database watermarks, and store is the SpanStore
        store.add(build_span(
            trace_id=make_trace_id(self.name, sid),
            span_id=make_span_id(self.name, sid, n),
            name="my.turn",
            span_kind="LLM",
            start_time=ts,
            attributes={"harness": self.name, "session.id": sid},
        ))
        return {self.name: (1, 0)}  # stage name -> (spans, errors)
```

Register the class in the `pyproject.toml` of your package:

```toml
[project.entry-points."agent_tap.harnesses"]
my-harness = "my_pkg.adapter:MyAdapter"
```

Follow these rules:

- Set the `harness` and `session.id` attributes on every span. The [schema](schema.md) lists the other attribute conventions.
- Use `build_span`. It redacts secrets and drops prompt text unless the user opted in.
- Record watermarks through `state` so each poll reads only new data, and open foreign databases read-only.
- Raise freely. agent-tap isolates a failing adapter and shows it in the `doctor` source health.
- Resolve data directories with `ctx.dir_for(name, default)`. Users can then override them in `[paths]` or with `AGENT_TAP_<NAME>_DIR`.
