import json
import re
import sqlite3
import time
from argparse import Namespace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from agent_tap import bugreport
from agent_tap.core.config import load_config
from agent_tap.emitter import SpanStore, build_span
from agent_tap.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml"
FAKE_TOKEN = "ghp_" + "a1B2c3D4e5" * 3


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "Users" / "alicedev"
    home.mkdir(parents=True)
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("USERNAME", "alicedev")
    monkeypatch.setenv("USER", "alicedev")
    for var in ("LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "HERMES_HOME", "CODEX_HOME", "AGENT_TAP_LOG",
                "AGENT_TAP_OMNIROUTE_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_TAP_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setattr("getpass.getuser", lambda: "alicedev")
    monkeypatch.setattr("os.getlogin", lambda: "alicedev")
    monkeypatch.setattr("agent_tap.adapters.omniroute.OmniRouteAdapter._docker", staticmethod(lambda: None))
    cfg = load_config(None, {"spans_db": tmp_path / "data" / "spans.db", "state_db": tmp_path / "state" / "state.sqlite",
                             "adapters_dir": tmp_path / "adapters"})
    return home, cfg


def form_ids(path: Path) -> list[str]:
    return re.findall(r"^\s+id:\s*(\w+)\s*$", path.read_text(encoding="utf-8"), re.M)


def test_bundle_on_empty_home(env):
    _, cfg = env
    b = bugreport.build_bundle(cfg)
    assert b["agent_tap_version"] and b["environment"]["python"]
    assert b["environment"]["install_method"] in bugreport.INSTALL_OPTIONS
    assert {s["name"] for s in b["services"]} >= {"raven", "hermes", "claude-code", "codex", "copilot-cli", "omniroute"}
    assert b["source_health"] == [] and b["log_tail"] == "" and b["error_text"] == "" and b["service_focus"] is None
    assert b["config"]["adapters_dir"] == "adapters" and "port" in b["config"]


def test_scrubs_home_username_and_secrets(env):
    home, _ = env
    text = (
        f"Traceback (most recent call last):\n  File \"{home}\\.venv\\lib\\x.py\", line 3, in run\n"
        f"  File \"{str(home).replace(chr(92), '/')}/proj/y.py\", line 9\n"
        f"  File \"C:\\Users\\bob\\other\\z.py\", line 1\nOSError: token {FAKE_TOKEN} for alicedev\n"
    )
    out = bugreport.scrub(text)
    assert str(home) not in out and "alicedev" not in out and "bob" not in out
    assert FAKE_TOKEN not in out and "[REDACTED]" in out
    assert out.count("~") >= 4 and "Traceback (most recent call last)" in out


def test_error_text_and_log_tail_are_scrubbed(env):
    home, cfg = env
    logs = cfg.state_db.parent.parent / "logs"
    logs.mkdir(parents=True)
    lines = [f"line {i}" for i in range(100)] + [f"crash in {home}\\agent.py with {FAKE_TOKEN}"]
    (logs / "agent-tap.err").write_text("\n".join(lines), encoding="utf-8")
    b = bugreport.build_bundle(cfg, error_text=f"boom at {home}/x token={FAKE_TOKEN}")
    assert FAKE_TOKEN not in json.dumps(b) and str(home) not in json.dumps(b)
    assert b["log_tail"].count("\n") <= 40 and "line 99" in b["log_tail"] and "line 10\n" not in b["log_tail"]


def test_source_health_and_service_focus(env):
    home, cfg = env
    st = StateStore(cfg.state_db)
    st.record_source_health("everos", spans_emitted=3, last_event_at=time.time(), last_error=f"failed in {home}")
    st.commit()
    st.close()
    store = SpanStore(cfg.spans_db)
    store.add(build_span(trace_id="t", span_id="s", name="service.probe", span_kind="SERVICE", status_code="ERROR",
                         attributes={"harness": "everos", "service.name": "everos", "service.status": "down",
                                     "http.status_code": 503, "error.class": "everos_down"}))
    store.flush()
    cfg.services = [{"name": "everos", "kind": "service", "health_url": "http://127.0.0.1:8000/health",
                     "api_token": "supersecretvalue123"}]
    b = bugreport.build_bundle(cfg, service="everos")
    assert b["source_health"][0]["source"] == "everos" and str(home) not in b["source_health"][0]["last_error"]
    focus = b["service_focus"]
    assert focus["spec"]["api_token"] == "[REDACTED]" and focus["spec"]["health_url"].endswith("/health")
    assert focus["latest_probe"]["service_status"] == "down" and focus["latest_probe"]["error_class"] == "everos_down"
    assert "supersecretvalue123" not in json.dumps(b)


def test_markdown_sections(env):
    _, cfg = env
    md = bugreport.render_markdown(bugreport.build_bundle(cfg, error_text="boom"))
    for heading in ("### Environment", "### Services", "### Source health", "### Config", "### Error output"):
        assert heading in md
    assert "```text\nboom\n```" in md


def test_issue_url_params_match_template_fields(env):
    _, cfg = env
    url = bugreport.issue_url(bugreport.build_bundle(cfg, service="searxng", error_text="HTTP 500 from engine"))
    parsed = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    ids = set(form_ids(TEMPLATE))
    assert parsed.path.endswith("/issues/new") and q["template"] == "bug_report.yml"
    assert set(q) - {"template", "title"} <= ids
    assert {"version", "os", "install_method", "service", "diagnostics"} <= set(q)
    assert q["service"] == "searxng" and q["title"].startswith("[bug] searxng: HTTP 500")


def test_dropdown_options_match_template(env):
    text = TEMPLATE.read_text(encoding="utf-8")
    for opt in bugreport.SERVICE_OPTIONS + bugreport.OS_OPTIONS + bugreport.INSTALL_OPTIONS:
        assert re.search(rf"^\s+- {re.escape(opt)}\s*$", text, re.M), opt


def test_unknown_service_falls_back(env):
    _, cfg = env
    url = bugreport.issue_url(bugreport.build_bundle(cfg, service="my-custom-thing"))
    assert parse_qs(urlparse(url).query)["service"][0] == "other harness"


def test_url_is_capped_and_notes_truncation(env):
    _, cfg = env
    big = "\n".join(f"File x{i}.py line {i} raised ValueError" for i in range(2000))
    b = bugreport.build_bundle(cfg, error_text=big)
    url = bugreport.issue_url(b)
    assert len(url) <= bugreport.MAX_URL
    assert "truncated" in parse_qs(urlparse(url).query)["diagnostics"][0]


def test_cli_prints_markdown_json_and_file(env, capsys, tmp_path):
    _, cfg = env
    tb = tmp_path / "tb.txt"
    tb.write_text("ValueError: bad value\n", encoding="utf-8")
    ns = Namespace(error_text="from flag", error_file=str(tb), service="firecrawl", open=False, json=False, output=None)
    assert bugreport.cmd_bug_report(cfg, ns) == 0
    out = capsys.readouterr().out
    assert "### Environment" in out and "from flag" in out and "ValueError: bad value" in out
    ns.json = True
    assert bugreport.cmd_bug_report(cfg, ns) == 0
    assert json.loads(capsys.readouterr().out)["service"] == "firecrawl"
    dest = tmp_path / "bundle.md"
    ns.json, ns.output = False, str(dest)
    assert bugreport.cmd_bug_report(cfg, ns) == 0
    assert "### Services" in dest.read_text(encoding="utf-8")


def test_cli_open_uses_browser(env, monkeypatch, capsys):
    _, cfg = env
    opened = []
    monkeypatch.setattr(bugreport.webbrowser, "open", lambda u: opened.append(u) or True)
    ns = Namespace(error_text=None, error_file=None, service=None, open=True, json=False, output=None)
    assert bugreport.cmd_bug_report(cfg, ns) == 0
    assert opened and opened[0].startswith(bugreport.ISSUE_URL)


def test_cli_missing_error_file(env, capsys):
    _, cfg = env
    ns = Namespace(error_text=None, error_file="no-such-file.txt", service=None, open=False, json=False, output=None)
    assert bugreport.cmd_bug_report(cfg, ns) == 2


def test_add_parser_registers_flags():
    import argparse

    p = argparse.ArgumentParser()
    bugreport.add_parser(p.add_subparsers(dest="cmd"))
    a = p.parse_args(["bug-report", "--service", "everos", "--error-text", "x", "--open", "--json"])
    assert a.service == "everos" and a.open and a.json


def test_issue_forms_are_valid_yaml():
    yaml = pytest.importorskip("yaml")
    for name in ("bug_report.yml", "service_adapter_request.yml", "config.yml"):
        data = yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / name).read_text(encoding="utf-8"))
        if name == "config.yml":
            assert data["blank_issues_enabled"] is False and len(data["contact_links"]) == 2
            continue
        assert data["name"] and data["description"] and data["body"]
        ids = [b["id"] for b in data["body"] if "id" in b]
        assert len(ids) == len(set(ids))
        for block in data["body"]:
            assert block["type"] in {"markdown", "input", "textarea", "dropdown", "checkboxes"}
    bug = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    assert bug["labels"] == ["bug", "needs-triage"] and bug["title"] == "[bug] "
    required = {b["id"] for b in bug["body"] if (b.get("validations") or {}).get("required")}
    assert required == {"service", "os", "version", "diagnostics", "steps"}
    assert {"service", "os", "install_method", "version", "config_type", "diagnostics", "error", "steps"} <= set(form_ids(TEMPLATE))
