import json
import sqlite3
from pathlib import Path

import pytest

from agent_tap import emitter
from agent_tap.adapters.base import Context, run_adapters
from agent_tap.core.config import Config
from agent_tap.emitter import SpanStore
from agent_tap.state import StateStore

ENV_VARS = ("AGENT_TAP_CONFIG", "AGENT_TAP_SPANS_DB", "AGENT_TAP_STATE_DB", "AGENT_TAP_STORE_PROMPTS",
            "AGENT_TAP_OMNIROUTE_DIR", "CODEX_HOME", "HERMES_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("LOCALAPPDATA", str(h / "AppData" / "Local"))
    monkeypatch.setenv("APPDATA", str(h / "AppData" / "Roaming"))
    return h


@pytest.fixture
def cfg(tmp_path, home):
    return Config(spans_db=tmp_path / "out" / "spans.db", state_db=tmp_path / "out" / "state.sqlite",
                  adapters_dir=tmp_path / "no-adapters")


@pytest.fixture
def run(cfg):
    """run(adapter, cutoff_ts=0, store_prompts=False) -> (stage results, span rows)."""

    def _run(adapter, cutoff_ts=0.0, store_prompts=False):
        emitter.set_store_prompt_text(store_prompts)
        state = StateStore(cfg.state_db)
        store = SpanStore(cfg.spans_db)
        raw = run_adapters([adapter], Context(cfg), state, store, cutoff_ts)
        res = {k: (v["spans"], v["errors"]) for k, v in raw.items()}
        store.flush()
        state.close()
        return res, spans(cfg)

    yield _run
    emitter.set_store_prompt_text(False)


def spans(cfg):
    if not cfg.spans_db.exists():
        return []
    con = sqlite3.connect(cfg.spans_db)
    try:
        return [{"name": n, "kind": k, "status": s, "attrs": json.loads(a)}
                for n, k, s, a in con.execute("SELECT name, span_kind, status_code, attributes FROM spans ORDER BY id")]
    finally:
        con.close()


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
