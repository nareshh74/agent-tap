import json
import sqlite3
import sys

import pytest

from agent_tap import emitter
from agent_tap.cli import main
from agent_tap.core import paths
from agent_tap.core.config import load_config
from agent_tap.emitter import SpanStore, build_span
from agent_tap.redact import redact_obj, redact_text


@pytest.fixture
def home(tmp_path, monkeypatch):
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(tmp_path))
    for var in ("LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "HERMES_HOME", "CODEX_HOME",
                "AGENT_TAP_CONFIG", "AGENT_TAP_SPANS_DB", "AGENT_TAP_STATE_DB", "AGENT_TAP_STORE_PROMPTS",
                "AGENT_TAP_OMNIROUTE_DIR", "AGENT_TAP_PORT", "AGENT_TAP_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_TAP_CONFIG", str(tmp_path / "none.toml"))
    return tmp_path


def test_redaction():
    assert "ghp_" in redact_text("token ghp_" + "a" * 30) and "a" * 30 not in redact_text("ghp_" + "a" * 30)
    assert redact_text("Authorization: Bearer abcdefgh12345678") == "Authorization: Bearer [REDACTED]"
    assert redact_obj({"api_key": "supersecretvalue", "ok": "x"}) == {"api_key": "[REDACTED]", "ok": "x"}


def test_prompt_text_off_by_default_and_opt_in(tmp_path):
    attrs = {"input.value": "hello secret plan", "output.value": "reply", "harness": "x", "tool.parameters": "{}"}
    emitter.set_store_prompt_text(False)
    sp = build_span(trace_id="t", span_id="s", name="n", attributes=attrs)
    assert "input.value" not in sp.attributes and "output.value" not in sp.attributes
    assert sp.attributes["input.value.length"] == len("hello secret plan")
    assert sp.attributes["harness"] == "x"
    emitter.set_store_prompt_text(True)
    try:
        sp = build_span(trace_id="t", span_id="s2", name="n", attributes=attrs)
        assert sp.attributes["input.value"] == "hello secret plan"
    finally:
        emitter.set_store_prompt_text(False)


def test_span_store_roundtrip(tmp_path):
    db = tmp_path / "spans.db"
    store = SpanStore(db)
    store.add(build_span(trace_id="t1", span_id="s1", name="a", attributes={"session.id": "S", "harness": "h"}))
    assert store.flush() == 1
    con = sqlite3.connect(db)
    assert con.execute("SELECT count(*) FROM spans").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM project_sessions").fetchone()[0] == 1


def test_config_precedence(home, monkeypatch):
    cfgfile = home / "c.toml"
    cfgfile.write_text("port = 7001\nstore_prompt_text = true\n[paths]\nomniroute = 'X'\n", encoding="utf-8")
    cfg = load_config(cfgfile)
    assert cfg.port == 7001 and cfg.store_prompt_text is True
    monkeypatch.setenv("AGENT_TAP_PORT", "7002")
    monkeypatch.setenv("AGENT_TAP_STORE_PROMPTS", "0")
    cfg = load_config(cfgfile, {"port": 7003})
    assert cfg.port == 7003 and cfg.store_prompt_text is False
    assert load_config(cfgfile).port == 7002
    assert str(cfg.harness_dir("omniroute")) == "X"
    assert load_config(home / "missing.toml").store_prompt_text is False


def test_path_discovery_empty_home(home):
    assert paths.claude_roots() == [] and paths.hermes_roots() == [] and paths.hermes_profiles() == []
    assert paths.omniroute_dirs() == []
    assert paths.codex_root() == home / ".codex"


def test_hermes_profiles_are_globbed(home):
    root = home / ".hermes"
    (root / "profiles" / "alpha").mkdir(parents=True)
    (root / "profiles" / "beta").mkdir()
    (root / "state.db").write_bytes(b"")
    assert [n for n, _ in paths.hermes_profiles()] == ["default", "alpha", "beta"]


def test_doctor_on_empty_home(home, capsys):
    rc = main(["--spans-db", str(home / "s.db"), "--state-db", str(home / "st.db"), "doctor", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["store_prompt_text"] is False
    assert out["spans_db"]["exists"] is False
    harnesses = {"raven", "hermes", "claude-code", "codex", "copilot-cli"}
    services = {"omniroute", "everos", "searxng", "freellmapi", "firecrawl"}
    assert set(out["adapters"]) >= harnesses | services
    assert all(out["adapters"][k] == "no data found" for k in harnesses)  # services may answer on a dev machine


def test_query_preset_and_readonly(home, capsys):
    db = home / "s.db"
    store = SpanStore(db)
    store.add(build_span(trace_id="t", span_id="s", name="n", status_code="ERROR", attributes={"harness": "h"}))
    store.flush()
    assert main(["--spans-db", str(db), "query", "harnesses", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["harness"] == "h"
    assert main(["--spans-db", str(db), "query", "DELETE FROM spans"]) == 1
    con = sqlite3.connect(db)
    assert con.execute("SELECT count(*) FROM spans").fetchone()[0] == 1


def test_config_show_and_run_once_empty(home, capsys):
    args = ["--spans-db", str(home / "s.db"), "--state-db", str(home / "st.db")]
    assert main(args + ["config", "show"]) == 0
    assert json.loads(capsys.readouterr().out)["port"] == 6007
    assert main(args + ["run", "--once"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_no_phoenix_or_otel_import():
    import subprocess

    code = "import sys, agent_tap.cli; print([m for m in sys.modules if m.startswith(('phoenix', 'opentelemetry'))])"
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True).stdout
    assert out.strip() == "[]"
