# Contributing

1. `git clone` the repo, then `python -m pip install -e ".[dev]"`.
2. Run `python -m pytest -q` before opening a PR. Tests use synthetic fixtures only: no personal paths, no real transcripts, no secrets.
3. New harness support: prefer a declarative TOML adapter (see `docs/writing-an-adapter.md`); use a Python adapter when you need custom parsing.
4. Keep runtime dependencies minimal (`platformdirs`, `fastapi`, `uvicorn`, stdlib).
5. Never commit databases, logs or config files containing real data.
