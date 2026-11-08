from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agent_tap.core.config import Config
from agent_tap.dashboard import create_dashboard_app
from agent_tap.emitter import SpanStore, build_span, set_store_prompt_text


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    cfg = Config(spans_db=tmp_path / "spans.db", state_db=tmp_path / "state.sqlite")
    set_store_prompt_text(True)
    store = SpanStore(cfg.spans_db)
    now = datetime.now(timezone.utc)
    store.add(build_span(trace_id="t1", span_id="s1", name="session.turn", span_kind="CHAIN", start_time=now - timedelta(minutes=5),
                         end_time=now - timedelta(minutes=4),
                         attributes={"harness": "claude", "session.id": "sess-1", "input.value": "hello"}))
    store.add(build_span(trace_id="t1", span_id="s2", parent_id="s1", name="tool.bash", span_kind="TOOL",
                         start_time=now - timedelta(minutes=4), end_time=now - timedelta(minutes=3), status_code="ERROR",
                         status_message="boom", attributes={"harness": "claude", "session.id": "sess-1", "tool.name": "bash",
                                                            "error.class": "tool_error"}))
    store.flush()
    # state db is created by StateStore on demand
    from agent_tap.state import StateStore
    StateStore(cfg.state_db).close()
    yield TestClient(create_dashboard_app(cfg))
    set_store_prompt_text(False)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_digest_counts_spans(client):
    d = client.get("/api/digest", params={"hours": 1}).json()
    assert d["summary"]["spans"] == 2
    assert [h["harness"] for h in d["harnesses"]] == ["claude"]
    assert "hermes_profiles" in d and "crons" in d


def test_trace_endpoints(client):
    d = client.get("/api/trace/t1").json()
    assert {s["span_id"] for s in d["spans"]} == {"s1", "s2"}
    anyd = client.get("/api/trace/_", params={"tool_name": "bash"}).json()
    assert [s["span_id"] for s in anyd["spans"]] == ["s2"]


def test_index_served_from_static(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="traceDrawer"' in r.text and "15 Profiles" not in r.text
