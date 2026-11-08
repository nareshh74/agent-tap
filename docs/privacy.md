# Privacy

agent-tap runs on your machine. It reads logs and databases that your agents and services already write, and it writes spans into a local SQLite file. The dashboard binds to `127.0.0.1` by default. The only network traffic agent-tap creates is the health probes you configure against your own services, and the Docker commands that read local container logs.

## Prompt and response text is off by default

The default is `store_prompt_text = false`. When it is off, agent-tap drops these attributes before it writes a span:

- `input.value`, `output.value` and `tool.parameters`
- every attribute that starts with `llm.input_messages`, `llm.output_messages`, `input.messages` or `output.messages`

When the dropped value was a string, agent-tap keeps its size as `<key>.length`, for example `input.value.length`, so size analytics still work. It keeps metadata such as the model, token counts, tool names, durations, status and error class.

To store the text, set the option in `config.toml`:

```toml
store_prompt_text = true
```

You can also set the environment variable `AGENT_TAP_STORE_PROMPTS=1`. The setting affects only spans collected afterwards. agent-tap does not rewrite existing rows.

## Secret redaction

Every span name, status message, attribute and event passes through `redact.py` before agent-tap stores it, including when prompt storage is on. The module masks these values:

- `Authorization: Bearer` headers and long bearer tokens
- GitHub tokens that start with `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` or `github_pat_`
- API keys that start with `sk-`, and Google keys that start with `AIza`
- JSON Web Tokens that start with `eyJ`
- the values of assignments to `api_key`, `access_token`, `refresh_token`, `client_secret`, `password`, `secret`, `cookie` and `x-api-key`
- dictionary values whose key contains `api_key`, `access_token`, `refresh_token`, `client_secret`, `password`, `cookie` or `authorization`

Redaction is pattern based, so it can miss a secret in an unusual format. Keep `store_prompt_text` off if you share the database.

## Service probes and logs

Service adapters send an HTTP GET to the `health_url` that you configure. They do not send request bodies or credentials. They read container logs with `docker logs` and log files from the paths you set. Request log lines pass through the same redaction as every other span, and agent-tap keeps the method, path, status code and latency, not request bodies.

## Bug reports

`agent-tap bug-report` collects your operating system, Python version, agent-tap version, enabled services, source health and recent errors from the agent-tap log. It masks secrets with the redaction rules above and replaces your home directory with `~`. It prints the report and sends nothing. You decide whether to post it. Read the report first, because an error message can contain project names or other text that no pattern recognizes.

## Data location

The default locations are per-user directories from `platformdirs`. The README lists them. To remove everything agent-tap stored, delete the spans and state files. To remove old data only, run `agent-tap prune --days N`.
