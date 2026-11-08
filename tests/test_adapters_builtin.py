import json
import sqlite3
import time

from agent_tap.adapters.base import Context, builtin_adapters
from agent_tap.adapters.claude_code import ClaudeCodeAdapter
from agent_tap.adapters.codex import CodexAdapter
from agent_tap.adapters.copilot_cli import CopilotCliAdapter
from agent_tap.adapters.hermes import HermesAdapter
from agent_tap.adapters.omniroute import OmniRouteAdapter
from agent_tap.adapters.raven import RavenAdapter

from conftest import write_jsonl

TS = "2026-01-02T03:04:05.000Z"


def _claude_prompt(home, text):
    write_jsonl(home / ".claude" / "projects" / "p" / "a.jsonl", [
        {"type": "user", "sessionId": "s", "timestamp": TS, "uuid": "u1",
         "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
    ])


def test_builtins_registered():
    names = set(builtin_adapters())
    assert {"claude-code", "codex", "copilot-cli", "hermes", "omniroute", "raven"} <= names
    assert {"everos", "searxng", "freellmapi", "firecrawl"} <= names
    for name, factory in builtin_adapters().items():
        assert factory().name == name


def test_discover_false_on_empty_home(home, cfg):
    ctx = Context(cfg)
    for cls in (ClaudeCodeAdapter, CodexAdapter, CopilotCliAdapter, HermesAdapter, RavenAdapter):
        assert cls().discover(ctx) is False


def test_claude_code(home, run):
    write_jsonl(home / ".claude" / "projects" / "proj" / "11111111-aaaa.jsonl", [
        {"type": "user", "sessionId": "sess-claude-1", "cwd": "/work/demo", "timestamp": TS, "uuid": "u1",
         "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
        {"type": "assistant", "sessionId": "sess-claude-1", "timestamp": TS, "uuid": "u2",
         "message": {"role": "assistant", "model": "claude-x", "content": [{"type": "text", "text": "hi"}],
                     "usage": {"input_tokens": 5, "output_tokens": 7}}},
    ])
    res, sp = run(ClaudeCodeAdapter(), 0)
    assert list(res) == ["claude"] and res["claude"][0] >= 1
    assert sp[0]["attrs"]["harness"] == "claude" and sp[0]["kind"] == "AGENT"


def test_codex(home, run):
    write_jsonl(home / ".codex" / "sessions" / "2026" / "rollout-x.jsonl", [
        {"type": "session_meta", "timestamp": TS,
         "payload": {"id": "thread-1", "session_id": "thread-1", "cwd": "/w", "model_provider": "openai", "timestamp": TS}},
    ])
    res, sp = run(CodexAdapter(), 0)
    assert res["codex"][0] >= 1
    assert sp[0]["attrs"]["harness"] == "codex" and sp[0]["attrs"]["child"]["session_id"] == "thread-1"


def test_copilot_cli(home, run):
    d = home / ".copilot" / "session-state" / "abcdef12-0000"
    d.mkdir(parents=True)
    (d / "workspace.yaml").write_text("name: demo task\ncwd: /w\nclient_name: github/cli\n", encoding="utf-8")
    res, sp = run(CopilotCliAdapter(), 0)
    assert res["copilot"][0] == 1
    assert sp[0]["attrs"]["harness"] == "copilot" and sp[0]["attrs"]["copilot"]["client_name"] == "github/cli"


def test_raven(home, run):
    node = home / ".raven" / "workspace" / "sessions" / "s1" / "subagents" / "nodes" / "c1.meta.json"
    node.parent.mkdir(parents=True)
    node.write_text(json.dumps({"session_key": "tui:s1", "call_id": "c1", "agent": "Raven-General", "status": "completed",
                                "started_at_ms": 1767323045000, "ended_at_ms": 1767323049000}), encoding="utf-8")
    res, sp = run(RavenAdapter(), 0)
    assert set(res) == {"raven_nodes", "raven_audit", "raven_sessions", "raven_logs"}
    assert res["raven_nodes"][0] >= 1
    assert any(s["attrs"].get("harness") == "raven" for s in sp)


def _hermes_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE sessions (id TEXT, source TEXT, created_source TEXT, parent_session_id TEXT, model TEXT, model_config TEXT,
      started_at REAL, ended_at REAL, last_activity_at REAL, end_reason TEXT, message_count INT, tool_call_count INT,
      input_tokens INT, output_tokens INT, cache_read_tokens INT, cache_write_tokens INT, reasoning_tokens INT,
      cwd TEXT, billing_provider TEXT, billing_base_url TEXT, title TEXT);
    CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
      tool_calls TEXT, tool_name TEXT, timestamp REAL, token_count INT, finish_reason TEXT, reasoning TEXT);
    """)
    now = time.time()
    con.execute("INSERT INTO sessions VALUES ('sess-h1','cli','cli',NULL,'m1','{}',?,?,?,'done',1,0,10,20,0,0,0,'/w','prov','','my title')",
                (now - 60, now, now))
    con.execute("INSERT INTO messages VALUES (1,'sess-h1','user','hello there',NULL,NULL,NULL,?,3,NULL,NULL)", (now - 50,))
    con.commit()
    con.close()


def test_hermes_discovers_profiles_dynamically(home, run):
    root = home / ".hermes"
    _hermes_db(root / "profiles" / "alpha" / "state.db")
    _hermes_db(root / "profiles" / "beta" / "state.db")
    res, sp = run(HermesAdapter(), 0)
    assert set(res) == {"hermes_db", "hermes_err"}
    profiles = {s["attrs"]["hermes"]["profile"] for s in sp if s["kind"] == "AGENT"}
    assert profiles == {"alpha", "beta"}


def test_omniroute(home, run, monkeypatch, tmp_path):
    d = tmp_path / "omni"
    d.mkdir()
    con = sqlite3.connect(d / "storage.sqlite")
    con.executescript("""
    CREATE TABLE call_logs (id TEXT, timestamp TEXT, method TEXT, path TEXT, status INT, model TEXT, requested_model TEXT,
      provider TEXT, duration INT, ttft_ms INT, tokens_in INT, tokens_out INT, tokens_cache_read INT, combo_name TEXT,
      error_summary TEXT, error_type TEXT, correlation_id TEXT, session_tag TEXT, response_id TEXT);
    CREATE TABLE usage_history (id INTEGER PRIMARY KEY, timestamp TEXT, provider TEXT, model TEXT, status TEXT, success INT,
      latency_ms INT, ttft_ms INT, tokens_input INT, tokens_output INT, error_code TEXT, endpoint TEXT, combo_strategy TEXT);
    """)
    con.execute("INSERT INTO call_logs VALUES ('c1','2026-01-02T03:04:05.000Z','POST','/v1/chat',500,'m','m','p',120,50,4,0,0,'combo','boom','e','corr','tag','r1')")
    con.commit()
    con.close()
    monkeypatch.setenv("AGENT_TAP_OMNIROUTE_DIR", str(d))
    monkeypatch.setenv("AGENT_TAP_OMNIROUTE_URL", "http://127.0.0.1:9/health")  # nothing listens: probe reports down
    res, sp = run(OmniRouteAdapter(), 0)
    assert res["omniroute"][0] >= 1 and res["omniroute_probe"][0] == 1
    err = [s for s in sp if (s["attrs"].get("http") or {}).get("status_code") == 500][0]
    assert err["status"] == "ERROR" and err["attrs"]["harness"] == "omniroute"
    probe = [s for s in sp if (s["attrs"].get("service") or {}).get("status")][0]
    assert probe["attrs"]["service"]["status"] == "down" and probe["attrs"]["service"]["kind"] == "gateway"


def test_store_prompt_text_off(home, run):
    _claude_prompt(home, "my private prompt")
    _, sp = run(ClaudeCodeAdapter(), 0, store_prompts=False)
    blob = json.dumps(sp)
    assert "my private prompt" not in blob
    assert any(s["attrs"].get("input", {}).get("value", {}).get("length") == len("my private prompt") for s in sp)


def test_store_prompt_text_on(home, run):
    _claude_prompt(home, "my private prompt")
    _, sp = run(ClaudeCodeAdapter(), 0, store_prompts=True)
    assert any(s["attrs"].get("input", {}).get("value") == "my private prompt" for s in sp)


def test_secret_redacted_even_when_prompts_stored(home, run):
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2"
    _claude_prompt(home, "token " + secret)
    _, sp = run(ClaudeCodeAdapter(), 0, store_prompts=True)
    blob = json.dumps(sp)
    assert secret not in blob and "REDACTED" in blob
