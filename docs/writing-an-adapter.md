# Writing an adapter

An adapter turns one harness's logs or databases into spans. There are two ways to add one.

## 1. Declarative TOML (no code)

Drop a `.toml` file into the adapters directory (`adapters_dir` in the config, default `<user config dir>/agent-tap/adapters/`), or check one first:

```bash
agent-tap adapters validate examples/adapters/aider.toml
agent-tap adapters list
```

Ready-made examples: `examples/adapters/aider.toml` (JSONL) and `examples/adapters/cline.toml` (JSON array; also works for Roo Code by changing the extension id in the glob).

### Schema

```toml
name = "my-harness"        # required; becomes the `harness` attribute and span-name prefix
kind = "jsonl"             # required: jsonl | json_array | sqlite

[source]
# jsonl / json_array
paths = ["~/.my-harness/*.jsonl"]   # globs; ~ and $ENV_VARS are expanded
# sqlite
db = "~/.my-harness/state.db"
query = "SELECT rowid AS id, * FROM events WHERE rowid > :last_rowid"  # must use :last_rowid; :cutoff (epoch seconds) is also bound
rowid_column = "id"        # column used as the incremental watermark (default "rowid")
table = "events"           # label used for the watermark

[map]                      # values are dotted paths into each record ("a.b.0.c")
timestamp = "time"         # required; epoch s/ms/ns or ISO-8601
session_id = "user_id"     # required
name = "event"             # span name (prefixed with the adapter name)
id = "uuid"                # stable span id source
end_timestamp = "ended"
span_kind = "kind"         # path to a kind value, or use span_kind_default = "CHAIN"
input = "prompt"           # only stored when store_prompt_text is on
output = "text"
model = "model"
tokens_prompt = "usage.in"
tokens_completion = "usage.out"
error = "error"            # truthy value marks the span ERROR

[where]                    # optional: only records where path == value
type = "say"
```

For file sources the pseudo-fields `_file` (file stem) and `_dir` (parent directory name) are available to `[map]`, useful as a session id. JSONL files are read incrementally by byte offset; JSON arrays by item count; SQLite by `:last_rowid`. Invalid files are skipped and reported by `adapters validate`.

## 2. Python adapter (entry point)

Implement the protocol from `agent_tap.adapters.base`:

```python
from agent_tap.adapters.base import Context, PollResult

class MyAdapter:
    name = "my-harness"

    def discover(self, ctx: Context) -> bool:
        return (ctx.dir_for(self.name) or Path.home() / ".my-harness").exists()

    def poll(self, ctx, state, store, cutoff_ts) -> PollResult:
        # state: StateStore (file/db watermarks); store: SpanStore
        from agent_tap.emitter import build_span
        from agent_tap.state import make_trace_id, make_span_id
        store.add(build_span(trace_id=make_trace_id(self.name, sid), span_id=make_span_id(self.name, sid, n),
                             name="my.turn", span_kind="LLM", start_time=ts,
                             attributes={"harness": self.name, "session.id": sid}))
        return {self.name: (1, 0)}   # stage -> (spans, errors)
```

Register it in your package's `pyproject.toml`:

```toml
[project.entry-points."agent_tap.harnesses"]
my-harness = "my_pkg.adapter:MyAdapter"
```

Rules of thumb:

- Always set `harness` and `session.id` attributes; see [schema.md](schema.md) for the other conventions.
- Use `build_span`, which redacts secrets and drops prompt text unless the user opted in.
- Be incremental: record watermarks via `state` so each poll only reads new data, and open foreign databases read-only.
- Raise freely: one failing adapter is isolated and shown in `doctor` source health.
- Resolve data dirs with `ctx.dir_for(name, default)` so users can override via `[paths]` or `AGENT_TAP_<NAME>_DIR`.
