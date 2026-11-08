# Contributing

1. Clone the repository, then run `python -m pip install -e ".[dev]"`.
2. Run `python -m pytest -q` before you open a pull request. Tests use synthetic fixtures only. They contain no personal paths, no real transcripts and no secrets.
3. To support a new harness, write a declarative TOML adapter first (see `docs/writing-an-adapter.md`). Write a Python adapter when you need custom parsing.
4. To support a new service, use `agent-tap onboard` or a `[[services]]` entry first (see `docs/writing-a-service-adapter.md`).
5. Keep the runtime dependencies to `platformdirs`, `fastapi`, `uvicorn` and the standard library.
6. Never commit databases, logs or config files that contain real data.
7. To report a bug, run `agent-tap bug-report` and open an issue with the bug report form. To request support for a new service or harness, use the service adapter request form.
