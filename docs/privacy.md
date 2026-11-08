# Privacy

agent-tap is local-first. It reads logs and databases that your agents already write on your machine, and writes spans into a local SQLite file. It makes no network calls (the dashboard binds to `127.0.0.1` by default).

## Prompt and response text is off by default

`store_prompt_text = false` is the default. When off, span builders drop these attributes before anything is written:

- `input.value`, `output.value`, `tool.parameters`
- anything starting with `llm.input_messages`, `llm.output_messages`, `input.messages`, `output.messages`

Where the dropped value was a string, its size is kept as `<key>.length` (for example `input.value.length`) so token/size analytics still work. Metadata such as model, token counts, tool names, durations, status and error class is kept.

Opt in with either:

```toml
# config.toml
store_prompt_text = true
```

or the environment: `AGENT_TAP_STORE_PROMPTS=1`.

Opting in only affects spans collected afterwards; existing rows are not rewritten.

## Secret redaction

Even with prompt storage on, every span name, status message, attribute and event passes through `redact.py` first. It masks:

- `Authorization: Bearer ...` headers and long bearer tokens
- GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`)
- `sk-...` style API keys and Google `AIza...` keys
- JWTs (`eyJ...`)
- values of `api_key`, `access_token`, `refresh_token`, `client_secret`, `password`, `secret`, `cookie`, `x-api-key` assignments
- dict values whose key name contains `api_key`, `access_token`, `refresh_token`, `client_secret`, `password`, `cookie` or `authorization`

Redaction is pattern based and best effort; keep `store_prompt_text` off if you share the database.

## Data location

Defaults are per-user directories from `platformdirs` (see the README). Delete the spans and state files to remove everything agent-tap has stored; `agent-tap prune --days N` removes old traces.
