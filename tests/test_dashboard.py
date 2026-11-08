import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agent_tap.core.config import Config
from agent_tap.dashboard import create_dashboard_app
from agent_tap.digest import generate_digest
from agent_tap.emitter import SpanStore, build_span, set_store_prompt_text
from agent_tap.state import StateStore


def _probe(store, span_id, name, status, minutes_ago, latency, code=200, endpoint="http://127.0.0.1:1/health"):
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=minutes_ago)
    store.add(build_span(
        trace_id=f"probe-{name}", span_id=span_id, name=f"{name}.probe", span_kind="SERVICE", start_time=start,
        end_time=start + timedelta(milliseconds=latency), status_code="OK" if status == "healthy" else "ERROR",
        attributes={"harness": name, "service.name": name, "service.kind": "service", "service.status": status,
                    "service.endpoint": endpoint, "service.latency_ms": latency, "http.status_code": code,
                    **({"error.class": f"{name}_down"} if status == "down" else {})}))


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    cfg = Config(spans_db=tmp_path / "spans.db", state_db=tmp_path / "state.sqlite",
                 services=[{"name": "mytool", "kind": "service", "health_url": "http://127.0.0.1:9/health"}])
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
    _probe(store, "p1", "everos", "healthy", 3, 12.0, endpoint="http://127.0.0.1:8000/health")
    _probe(store, "p2", "everos", "healthy", 1, 20.0, endpoint="http://127.0.0.1:8000/health")
    _probe(store, "p3", "searxng", "healthy", 8, 40.0)
    _probe(store, "p4", "searxng", "down", 2, 5000.0, code=503)
    store.add(build_span(trace_id="req-searxng", span_id="r1", name="searxng.request", span_kind="SERVICE",
                         start_time=now - timedelta(minutes=2), end_time=now - timedelta(minutes=2) + timedelta(milliseconds=300),
                         attributes={"harness": "searxng", "service.kind": "service", "http.method": "GET",
                                     "http.path": "/search", "http.status_code": 200}))
    # Legacy gateway span without service.* attributes: the kind comes from the name.
    store.add(build_span(trace_id="om1", span_id="o1", name="omniroute.call", span_kind="LLM", start_time=now - timedelta(minutes=1),
                         end_time=now - timedelta(seconds=50), attributes={"harness": "omniroute"}))
    store.flush()
    StateStore(cfg.state_db).close()
    yield cfg
    set_store_prompt_text(False)


@pytest.fixture()
def client(cfg):
    return TestClient(create_dashboard_app(cfg))


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_harnesses_exclude_services(client):
    d = client.get("/api/digest", params={"hours": 1}).json()
    assert [h["harness"] for h in d["harnesses"]] == ["claude"]
    assert "crons" in d and "hermes_profiles" not in d
    assert d["summary"]["spans"] == 8


def test_services_rows(client):
    d = client.get("/api/digest", params={"hours": 1}).json()
    rows = {r["service"]: r for r in d["services"]}
    assert {"everos", "searxng", "omniroute", "mytool"} <= set(rows)
    assert rows["everos"]["status"] == "healthy" and rows["everos"]["checks"] == 2 and rows["everos"]["errors"] == 0
    assert rows["everos"]["endpoint"] == "http://127.0.0.1:8000/health"
    assert rows["searxng"]["status"] == "down" and rows["searxng"]["checks"] == 2
    assert rows["searxng"]["requests"] == 1 and rows["searxng"]["errors"] == 1
    assert rows["searxng"]["trace_id"] == "probe-searxng"
    assert rows["searxng"]["p50_ms"] == pytest.approx(300.0, abs=60)
    assert rows["omniroute"]["kind"] == "gateway" and rows["omniroute"]["requests"] == 1
    assert rows["mytool"]["status"] == "unknown" and rows["mytool"]["endpoint"] == "http://127.0.0.1:9/health"
    assert d["service_summary"] == {"total": 4, "healthy": 1, "degraded": 0, "down": 1, "unknown": 2}
    for key in ("service", "kind", "status", "endpoint", "checks", "requests", "errors", "p50_ms", "p90_ms", "last_seen", "trace_id"):
        assert key in rows["everos"]


def test_dropdown_choices_survive_a_filter(client):
    d = client.get("/api/digest", params={"hours": 1, "harness": "everos"}).json()
    assert [r["service"] for r in d["services"]] == ["everos"]
    assert d["harnesses"] == []
    assert d["available"]["harnesses"] == ["claude"]
    assert {x["name"] for x in d["available"]["services"]} == {"everos", "searxng", "omniroute", "mytool"}


def test_trace_endpoints(client):
    d = client.get("/api/trace/t1").json()
    assert {s["span_id"] for s in d["spans"]} == {"s1", "s2"}
    anyd = client.get("/api/trace/_", params={"tool_name": "bash"}).json()
    assert [s["span_id"] for s in anyd["spans"]] == ["s2"]


def test_index_has_unnumbered_titles_and_no_hermes_breakdown(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="traceDrawer"' in r.text and 'id="reportBtn"' in r.text
    titles = re.findall(r"<h2[^>]*>(.*?)</h2>", r.text, re.S)
    assert "Harness Activity" in titles and "Running Services &amp; Gateways" in titles
    assert not [t for t in titles if re.match(r"\s*\d+[A-Z]?\.", t)], titles
    assert "hermes_profiles" not in r.text and "hermesProfileTable" not in r.text and "Profile Breakdown" not in r.text
    assert "15 Profile" not in r.text and "15-Profile" not in r.text
    assert 'id="servicesTable"' in r.text


def test_bug_report_endpoint(client):
    pytest.importorskip("agent_tap.bugreport")
    d = client.get("/api/bug-report", params={"service": "searxng"}).json()
    assert d["markdown"] and d["url"].startswith("https://github.com/")


def test_digest_lists_services_and_drops_numbers(cfg):
    out = generate_digest(hours=1, phoenix_db=cfg.spans_db, state_db=cfg.state_db, service_kinds=cfg.service_kinds())
    assert "## Running Services & Gateways" in out and "| **searxng** | service | down |" in out
    assert not re.search(r"^## \d", out, re.M)
    harness_part = out.split("## Running Services")[0]
    assert "**claude**" in harness_part and "**everos**" not in harness_part
    import json

    d = json.loads(generate_digest(hours=1, phoenix_db=cfg.spans_db, state_db=cfg.state_db, as_json=True))
    assert set(d["harnesses"]) == {"claude"} and {s["service"] for s in d["services"]} == {"everos", "searxng", "omniroute"}
