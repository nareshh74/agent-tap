import argparse
import json
import sqlite3
import subprocess
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_tap.adapters import service as svc
from agent_tap.adapters.base import Context, adapter_kind, load_adapters
from agent_tap import onboard
from agent_tap.core.config import Config, load_config
from agent_tap.emitter import SpanStore
from agent_tap.state import StateStore

NS = "2026-11-08T15:54:%02d.000000000Z"


@pytest.fixture
def home(tmp_path, monkeypatch):
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(tmp_path))
    for var in ("LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "AGENT_TAP_CONFIG", "AGENT_TAP_OMNIROUTE_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_TAP_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    monkeypatch.setattr("agent_tap.adapters.omniroute.OmniRouteAdapter._docker", staticmethod(lambda: None))
    return tmp_path


class _Handler(BaseHTTPRequestHandler):
    status = 200
    body = b'{"status":"ok","version":"1.2.3"}'

    def do_GET(self):  # noqa: N802
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    _Handler.status, _Handler.body = 200, b'{"status":"ok","version":"1.2.3"}'
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/health"
    srv.shutdown()


class FakeDocker:
    def __init__(self):
        self.containers: dict[str, dict] = {}

    def run(self, cmd, timeout=20.0):
        cp = lambda code, out="": subprocess.CompletedProcess(cmd, code, out, "")  # noqa: E731
        name = cmd[-1]
        c = self.containers.get(name)
        if cmd[:2] == ["docker", "inspect"]:
            return cp(0, f"{c['status']}|{c.get('health', '')}\n") if c else cp(1)
        if cmd[:2] == ["docker", "logs"] and c:
            since = cmd[cmd.index("--since") + 1]
            lines = [x for x in c["lines"] if "T" not in since or x.split(" ", 1)[0] >= since]
            return cp(0, "\n".join(lines) + "\n")
        return cp(1)


@pytest.fixture
def docker(monkeypatch):
    fd = FakeDocker()
    monkeypatch.setattr(svc, "_run", fd.run)
    monkeypatch.setattr(svc, "docker_available", lambda: True)
    return fd


@pytest.fixture
def rt(tmp_path):
    cfg = Config(spans_db=tmp_path / "s.db", state_db=tmp_path / "st.db")
    store, state = SpanStore(cfg.spans_db), StateStore(cfg.state_db)
    return Context(cfg), state, store


def poll(adapter, rt_, cutoff=0.0):
    ctx, state, store = rt_
    res = adapter.poll(ctx, state, store, cutoff)
    store.flush()
    state.commit()
    return res


def spans(rt_, where="1=1"):
    con = sqlite3.connect(rt_[2].db_path)
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(f"SELECT name, span_kind, status_code, status_message, attributes FROM spans WHERE {where}")]


def attrs(row):
    return json.loads(row["attributes"])


# ---------------------------------------------------------------- probing


def test_probe_healthy_degraded_down_timeout(server, monkeypatch):
    ad = svc.ServiceAdapter({"name": "demo", "health_url": server})
    ok = ad.probe()
    assert ok.status == "healthy" and ok.http_status == 200 and ok.extra["demo.version"] == "1.2.3"
    _Handler.status = 503
    bad = ad.probe()
    assert (bad.status, bad.error_class) == ("degraded", "demo_http_5xx")
    _Handler.status, _Handler.body = 200, b'{"status":"ok","cascade":{"healthy":false,"pending":4}}'
    casc = ad.probe()
    assert (casc.status, casc.error_class) == ("degraded", "demo_degraded") and casc.extra["demo.pending"] == 4
    down = svc.ServiceAdapter({"name": "demo", "health_url": "http://127.0.0.1:9/health"}).probe()
    assert (down.status, down.error_class) == ("down", "demo_down")
    monkeypatch.setattr(svc, "http_probe", lambda *a, **k: (None, 3000.0, "timeout", "timed out"))
    assert ad.probe().error_class == "demo_timeout"


def test_probe_span_attributes_and_throttle(server, rt):
    ad = svc.ServiceAdapter({"name": "demo", "kind": "gateway", "health_url": server, "probe_interval": 3600})
    assert poll(ad, rt) == {"demo": (1, 0)}
    assert poll(ad, rt) == {"demo": (0, 0)}  # inside the probe interval
    (row,) = spans(rt)
    a = attrs(row)
    assert row["span_kind"] == "SERVICE" and row["status_code"] == "OK"
    assert a["harness"] == "demo" and a["service"]["kind"] == "gateway" and a["service"]["status"] == "healthy"
    assert a["http"]["status_code"] == 200 and a["service"]["endpoint"] == server.split("?")[0]
    assert "latency_ms" in a["service"]


def test_down_service_emits_error_probe_and_discover_when_configured(home, rt):
    ad = svc.ServiceAdapter({"name": "demo", "health_url": "http://127.0.0.1:9/health"})
    assert ad.discover(rt[0]) is False
    ad._explicit = True
    assert ad.discover(rt[0]) is True
    assert poll(ad, rt) == {"demo": (1, 1)}
    (row,) = spans(rt)
    assert row["status_code"] == "ERROR" and attrs(row)["error"]["class"] == "demo_down"


def test_process_and_container_probes(docker, monkeypatch, rt):
    monkeypatch.setattr(svc, "process_running", lambda n: n == "up.exe")
    assert svc.ServiceAdapter({"name": "p", "process_name": "up.exe"}).probe().status == "healthy"
    assert svc.ServiceAdapter({"name": "p", "process_name": "no.exe"}).probe().error_class == "p_down"
    docker.containers["c1"] = {"status": "running", "health": "unhealthy", "lines": []}
    assert svc.ServiceAdapter({"name": "c", "docker_container": "c1"}).probe().status == "degraded"
    docker.containers["c1"]["status"] = "exited"
    assert svc.ServiceAdapter({"name": "c", "docker_container": "c1"}).probe().status == "down"
    assert svc.ServiceAdapter({"name": "c", "docker_container": "ghost"}).probe().error_class == "c_container_missing"


# ---------------------------------------------------------------- built-ins with realistic logs

FIRECRAWL = [
    NS % 1 + " \x1b[36mapi\x1b[0m            2026-11-08 15:54:01 \x1b[32minfo\x1b[39m [ScrapeURL:]: Scrape via playwright deemed successful. ",
    NS % 2 + " api            2026-11-08 15:54:02 info [:POST]: v1 request completed ",
    NS % 3 + " api            2026-11-08 15:54:03 warn [:]: DuckDuckGo: Encountered anti-bot measures, retrying... {\"attempt\":2,\"term\":\"my secret query\"}",
    NS % 4 + ' api            2026-11-08 15:54:04 error [:]: DuckDuckGo: Blocked by anti-bot measures {"term":"my secret query"}',
    NS % 5 + " nuq-reconciler 2026-11-08 15:54:05 info [:]: Concurrency queue reconciler run complete ",
    NS % 6 + " api            2026-11-08 15:54:06 error [nuq/rabbitmq:]: connection timeout while publishing",
]


def firecrawl_docker(docker):
    docker.containers["firecrawl-api-1"] = {"status": "running", "lines": list(FIRECRAWL)}
    for c in ("playwright-service", "nuq-postgres", "redis"):
        docker.containers[f"firecrawl-{c}-1"] = {"status": "running", "health": "healthy", "lines": []}
    docker.containers["firecrawl-rabbitmq-1"] = {"status": "exited", "lines": []}


def test_firecrawl_parses_logs_and_inspects_components(docker, server, rt):
    firecrawl_docker(docker)
    ad = svc.ServiceAdapter({"name": "firecrawl", "health_url": server})
    assert ad.kind == "service" and ad.spec.parser == "firecrawl"
    poll(ad, rt)
    rows = spans(rt)
    names = {r["name"] for r in rows}
    assert "firecrawl.request POST /v1" in names and "firecrawl.scrape [playwright]" in names
    assert not any("reconciler" in n for n in names)
    classes = {attrs(r).get("error", {}).get("class") for r in rows}
    assert {"firecrawl_search_blocked", "firecrawl_timeout", "firecrawl_warning"} <= classes
    probes = {r["name"]: r for r in rows if ".probe" in r["name"]}
    assert probes["firecrawl.probe [healthy]"]["status_code"] == "OK"
    assert probes["firecrawl.rabbitmq.probe [down]"]["status_code"] == "ERROR"
    assert attrs(probes["firecrawl.redis.probe [healthy]"])["service"]["component"] == "redis"
    assert "my secret query" not in json.dumps(rows)


def test_log_watermark_prevents_duplicates(docker, server, rt):
    firecrawl_docker(docker)
    ad = svc.ServiceAdapter({"name": "firecrawl", "health_url": server, "probe_interval": 3600})
    poll(ad, rt)
    first = len(spans(rt))
    poll(ad, rt)
    assert len(spans(rt)) == first
    docker.containers["firecrawl-api-1"]["lines"].append(NS % 9 + " api  2026-11-08 15:54:09 info [:GET]: v2 request completed ")
    assert poll(ad, rt)["firecrawl"][0] == 1
    assert len(spans(rt)) == first + 1


SEARX = [
    NS % 1 + " 2026-11-08 15:52:12,108 WARNING:searx.network.brave: HTTP Request failed: GET https://search.brave.com/search?q=my+private+terms&source=web",
    NS % 2 + " 2026-11-08 15:52:12,112 WARNING:searx.engines.brave: ErrorContext('a.py', 211, 'x', 'searx.exceptions.SearxEngineTooManyRequestsException', None, ()) False",
    NS % 3 + " 2026-11-08 15:52:55,496 WARNING:searx.engines.wikipedia: ErrorContext('w.py', 180, 'y', 'curl_cffi.requests.exceptions.HTTPError', None, ('400', '', 'en.wikipedia.org')) False",
    NS % 4 + " 2026-11-08 15:53:01,000 ERROR:searx.engines.duckduckgo: ReadTimeout: timed out",
    NS % 5 + ' 10.0.0.1 - - [08/Nov/2026] "GET /search?q=private+terms HTTP/1.1" 500 12',
    NS % 6 + " 2026-11-08 15:53:02,000 INFO:searx.webapp: ordinary info line",
]


def test_searxng_engine_error_classes_and_scrubbed_queries(docker, server, rt):
    docker.containers["searxng"] = {"status": "running", "lines": SEARX}
    ad = svc.ServiceAdapter({"name": "searxng", "health_url": server})
    poll(ad, rt)
    rows = [r for r in spans(rt) if ".probe" not in r["name"]]
    classes = sorted(attrs(r)["error"]["class"] for r in rows)
    assert classes == ["searxng_engine_http_400", "searxng_engine_rate_limited", "searxng_engine_timeout",
                       "searxng_http_5xx", "searxng_network_error"]
    blob = json.dumps(rows)
    assert "private" not in blob and "q=" not in blob
    req = next(r for r in rows if r["name"].startswith("searxng.request"))
    assert attrs(req)["http"] == {"method": "GET", "path": "/search", "status_code": 500}


FREELLM = [
    NS % 1 + " [Proxy] 15:54:44 start 602984 a0 google - gemini-3.5-flash req=claude-sonnet-4-5",
    NS % 2 + " [Proxy] 15:54:57 ok 602984 a0 google - gemini-3.5-flash lat=13605ms in=5234 out=777",
    NS % 3 + ' [Proxy] 15:47:45 fail d1e499 a0 groq - qwen/qwen3.8-27b lat=86ms err="Groq API error 429: Request too large in organization `org_01abcXYZ` tier"',
    NS % 4 + ' [Proxy] 15:59:28 fail 41e9d0 a0 google - gemini-3.5-flash lat=1642ms err="Google API error 400: bad turn order"',
    NS % 5 + " [Proxy] 15:59:29 next 41e9d0 a2 nvidia - poolside/laguna",
    NS % 6 + " [Health] Checking 16 keys...",
    NS % 7 + " [FallbackLoop] chat completions routing exhausted (no upstream tried) req=cdb31e requested=x",
]


def test_freellmapi_completions_become_llm_spans(docker, server, rt):
    docker.containers["freellmapi-freellmapi-1"] = {"status": "running", "health": "healthy", "lines": FREELLM}
    ad = svc.ServiceAdapter({"name": "freellmapi", "health_url": server})
    assert ad.kind == "gateway"
    poll(ad, rt)
    rows = [r for r in spans(rt) if ".probe" not in r["name"]]
    ok = next(r for r in rows if r["status_code"] == "OK")
    a = attrs(ok)
    assert ok["span_kind"] == "LLM" and a["llm"]["token_count"] == {"prompt": 5234, "completion": 777}
    assert a["llm"]["provider"] == "google" and a["service"]["latency_ms"] == 13605
    by_class = {attrs(r)["error"]["class"]: r for r in rows if r["status_code"] == "ERROR"}
    assert set(by_class) == {"freellmapi_rate_limited", "freellmapi_upstream_400", "freellmapi_routing_exhausted"}
    assert "org_01abcXYZ" not in json.dumps(rows)
    assert len(rows) == 4  # start, next and Health lines are not spans


def test_everos_probe_only(server, rt):
    ad = svc.ServiceAdapter({"name": "everos", "health_url": server})
    assert ad.kind == "service" and ad.spec.docker_containers == []
    assert poll(ad, rt) == {"everos": (1, 0)}


# ---------------------------------------------------------------- generic services and config


def test_generic_log_file_with_format_is_incremental(tmp_path, rt):
    log = tmp_path / "app.log"
    log.write_text('"GET /a?x=1 HTTP/1.1" 200 12ms\n"POST /b HTTP/1.1" 502 900ms\n', encoding="utf-8")
    fmt = r'"(?P<method>GET|POST) (?P<path>\S+) HTTP/1\.1" (?P<status>\d{3}) (?P<duration_ms>\d+)ms'
    ad = svc.ServiceAdapter({"name": "myapp", "log_path": str(log), "log_format": fmt})
    assert poll(ad, rt)["myapp"] == (3, 1)  # probe + two requests
    assert poll(ad, rt, 0)["myapp"][0] == 0
    with open(log, "a", encoding="utf-8") as f:
        f.write('"GET /c HTTP/1.1" 200 5ms\n')
    assert poll(ad, rt)["myapp"][0] == 1
    rows = spans(rt, "name LIKE 'myapp.request%'")
    bad = next(r for r in rows if "502" in r["name"])
    a = attrs(bad)
    assert a["http"]["path"] == "/b" and a["error"]["class"] == "myapp_http_5xx" and a["service"]["latency_ms"] == 900


def test_generic_error_lines_without_format(tmp_path, rt):
    log = tmp_path / "w.log"
    log.write_text("starting\nERROR db connection lost\nTraceback (most recent call last)\nok\n", encoding="utf-8")
    ad = svc.ServiceAdapter({"name": "w", "log_path": str(log)})
    assert poll(ad, rt)["w"] == (3, 2)
    assert {attrs(r)["error"]["class"] for r in spans(rt, "name = 'w.log_error'")} == {"w_log_error"}


def test_config_services_and_builtin_override(home, tmp_path):
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(
        'enabled_services = ["searxng", "myapi"]\n'
        '[[services]]\nname = "searxng"\nhealth_url = "http://127.0.0.1:1234/healthz"\n'
        '[[services]]\nname = "myapi"\nkind = "gateway"\nhealth_url = "http://127.0.0.1:9000/h"\n'
        '[[services]]\nname = "broken"\nkind = "nonsense"\n', encoding="utf-8")
    cfg = load_config(cfg_file)
    ads = {a.name: a for a in load_adapters(cfg)}
    assert set(ads) == {"searxng", "myapi"}  # enabled_services filters; invalid entry skipped
    assert ads["searxng"].spec.health_url == "http://127.0.0.1:1234/healthz"
    assert ads["searxng"].spec.docker_containers == ["searxng"]  # built-in defaults kept
    assert ads["myapi"].kind == "gateway" and adapter_kind(ads["myapi"]) == "gateway"
    assert sum(1 for a in load_adapters(Config(spans_db=tmp_path / "x", state_db=tmp_path / "y")) if a.name == "searxng") == 1


def test_load_adapters_includes_four_services_with_kinds(home):
    cfg = Config(spans_db=home / "s.db", state_db=home / "st.db")
    kinds = {a.name: adapter_kind(a) for a in load_adapters(cfg)}
    assert {k: kinds[k] for k in ("everos", "searxng", "freellmapi", "firecrawl")} == {
        "everos": "service", "searxng": "service", "freellmapi": "gateway", "firecrawl": "service"}
    cfg.enabled_services = ["everos", "codex"]
    assert {a.name for a in load_adapters(cfg)} == {"everos", "codex"}


def test_scrub_removes_queries_terms_and_org_ids():
    text = '\x1b[31mGET https://x.example/search?q=secret&a=1 {"term":"a \\"b\\" c"} org_01ZZ\x1b[0m'
    out = svc.scrub(text)
    assert "secret" not in out and "a=1" not in out and "org_01ZZ" not in out and "\x1b" not in out
    assert svc.safe_endpoint("http://user:pw@127.0.0.1:8000/health?token=abc") == "http://127.0.0.1:8000/health"


# ---------------------------------------------------------------- onboard command


def onb(argv):
    parser = argparse.ArgumentParser()
    onboard.add_parser(parser.add_subparsers(dest="cmd"))
    return onboard.cmd_onboard(load_config(None), parser.parse_args(argv))


def test_onboard_non_interactive_writes_valid_toml(home, server, capsys):
    cfg_path = home / "cfg" / "config.toml"
    cfg_path.parent.mkdir()
    cfg_path.write_text('enabled_harnesses = ["codex"]\nport = 7001\n', encoding="utf-8")
    rc = onb(["onboard", "--name", "myapi", "--kind", "gateway", "--health-url", server, "--docker-container", "c1",
               "--docker-container", "c2", "--non-interactive"])
    assert rc == 0 and "Probe: myapi is healthy" in capsys.readouterr().out
    data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["port"] == 7001 and data["enabled_services"] == ["codex", "myapi"] and "enabled_harnesses" not in data
    assert data["services"] == [{"name": "myapi", "kind": "gateway", "health_url": server, "docker_container": ["c1", "c2"]}]
    assert cfg_path.with_name("config.toml.bak").exists()
    assert {a.name for a in load_adapters(load_config(cfg_path))} == {"codex", "myapi"}


def test_onboard_rejects_bad_input_duplicates_and_replaces(home, server, capsys):
    base = ["onboard", "--non-interactive"]
    assert onb(base) == 2  # no name
    assert onb(base + ["--name", "x"]) == 2  # nothing to monitor
    assert onb(base + ["--name", "bad name", "--health-url", server]) == 2
    assert onb(base + ["--name", "x", "--health-url", "ftp://h"]) == 2
    capsys.readouterr()
    assert onb(base + ["--name", "x", "--health-url", server]) == 0
    assert onb(base + ["--name", "x", "--health-url", server]) == 2
    assert "already configured" in capsys.readouterr().err
    assert onb(base + ["--name", "x", "--health-url", "http://127.0.0.1:9/h", "--replace"]) == 0
    out = capsys.readouterr().out
    assert "is down (x_down)" in out
    data = tomllib.loads((home / "cfg" / "config.toml").read_text(encoding="utf-8"))
    assert len(data["services"]) == 1 and data["services"][0]["health_url"] == "http://127.0.0.1:9/h"


def test_service_add_alias_and_log_format_roundtrip(home, capsys):
    fmt = r'"(?P<method>GET) (?P<path>\S+)" (?P<status>\d{3})'
    assert onb(["service-add", "--name", "w", "--log-path", str(home / "w.log"), "--log-format", fmt, "--yes"]) == 0
    data = tomllib.loads((home / "cfg" / "config.toml").read_text(encoding="utf-8"))
    assert data["services"][0]["log_format"] == fmt


def test_adapters_validate_accepts_service_files(tmp_path, capsys):
    from agent_tap.cli import main

    good = tmp_path / "svc.toml"
    good.write_text('[[services]]\nname = "billing-api"\nkind = "service"\nhealth_url = "http://127.0.0.1:9000/health"\n', encoding="utf-8")
    assert main(["adapters", "validate", str(good)]) == 0
    bad = tmp_path / "bad.toml"
    bad.write_text('[[services]]\nname = "x"\nkind = "weird"\n', encoding="utf-8")
    assert main(["adapters", "validate", str(bad)]) == 1
