"""Service adapters: monitor any running service (HTTP endpoint, Docker container, log file, process).

A service has a kind: "harness", "gateway" or "service". Each poll emits a SERVICE probe span (status, latency)
and one span per request or error found in the service's Docker or file logs. Built-in adapters cover
everos, searxng, freellmapi and firecrawl. Any other service is described in config.toml with [[services]].
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from ..core.config import Config
from ..core.schema import SERVICE_KINDS
from ..emitter import SpanStore, build_span, ensure_utc
from ..state import StateStore, make_span_id, make_trace_id
from .base import Context, PollResult

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DOCKER_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z) ?(.*)$", re.S)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DEFAULT_PROBE_INTERVAL = 30.0
MAX_LOG_LINES = 5000


# ---------------------------------------------------------------- helpers


def scrub(text: str) -> str:
    """Remove search terms, URL query strings and organization ids from log text before it becomes a span."""
    text = ANSI_RE.sub("", text)
    text = re.sub(r"\"term\":\s*\"(?:[^\"\\]|\\.)*\"", '"term":"[omitted]"', text)
    text = re.sub(r"(https?://[^\s?'\"]+)\?[^\s'\"]*", r"\1?[query omitted]", text)
    text = re.sub(r"\borg_[A-Za-z0-9]+", "org_[redacted]", text)
    return text.strip()


def safe_endpoint(url: str) -> str:
    """Drop credentials and query string from a URL."""
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        if p.port:
            host = f"{host}:{p.port}"
        return urlunsplit((p.scheme, host, p.path, "", ""))
    except ValueError:
        return ""


@dataclass
class ProbeResult:
    status: str  # healthy | degraded | down
    http_status: Optional[int] = None
    latency_ms: float = 0.0
    error_class: Optional[str] = None
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def http_probe(url: str, timeout: float = 3.0) -> tuple[Optional[int], float, Optional[str], str]:
    """Return (http_status, latency_ms, error_kind, body) where error_kind is None, "timeout" or "unreachable"."""
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "agent-tap/health"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (operator-supplied local URL)
            body = r.read(4096).decode("utf-8", "replace")
            return r.status, (time.perf_counter() - t0) * 1000.0, None, body
    except urllib.error.HTTPError as e:
        try:
            body = e.read(1024).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, (time.perf_counter() - t0) * 1000.0, None, body
    except (TimeoutError, OSError, urllib.error.URLError) as e:
        reason = getattr(e, "reason", e)
        kind = "timeout" if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower() else "unreachable"
        return None, (time.perf_counter() - t0) * 1000.0, kind, str(reason)[:200]


def _run(cmd: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None


def docker_available() -> bool:
    return shutil.which("docker") is not None


def docker_state(container: str) -> Optional[dict[str, str]]:
    """Return {"status", "health"} for a container, or None when docker or the container is missing."""
    r = _run(["docker", "inspect", "-f", "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}", container], 10.0)
    if r is None or r.returncode != 0 or not r.stdout.strip():
        return None
    status, _, health = r.stdout.strip().splitlines()[-1].partition("|")
    return {"status": status, "health": health}


def docker_logs(container: str, since: str) -> list[str]:
    """Return timestamped log lines (stdout and stderr) since an RFC3339 string or unix time."""
    r = _run(["docker", "logs", "-t", "--since", since, container], 30.0)
    if r is None or r.returncode != 0:
        return []
    return ((r.stdout or "") + (r.stderr or "")).splitlines()[-MAX_LOG_LINES:]


def process_running(name: str) -> bool:
    if os.name == "nt":
        r = _run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"], 10.0)
        return bool(r and r.returncode == 0 and name.lower() in r.stdout.lower())
    r = _run(["pgrep", "-f", name], 10.0)
    return bool(r and r.returncode == 0 and r.stdout.strip())


# ---------------------------------------------------------------- log parsing


@dataclass
class LogEvent:
    ts: datetime
    name: str
    status: str = "OK"  # OK | ERROR | UNSET
    span_kind: str = "SERVICE"
    method: str = ""
    path: str = ""
    http_status: Optional[int] = None
    duration_ms: Optional[float] = None
    error_class: Optional[str] = None
    message: str = ""
    attrs: dict[str, Any] = field(default_factory=dict)


Parser = Callable[[str, datetime, str], Optional[LogEvent]]  # (service, docker_ts, body) -> event

FIRECRAWL_RE = re.compile(r"^(?P<svc>\S+)\s+(?P<d>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+(?P<lvl>\w+)\s+\[(?P<ctx>[^\]]*)\]:\s*(?P<msg>.*)$")


def parse_firecrawl(service: str, ts: datetime, body: str) -> Optional[LogEvent]:
    m = FIRECRAWL_RE.match(ANSI_RE.sub("", body))
    if not m:
        return None
    lvl, ctx, msg = m["lvl"].lower(), m["ctx"], m["msg"]
    if "nuq-reconciler" in m["svc"]:
        return None
    http = re.search(r":(POST|GET|PUT|DELETE|PATCH)$", ctx)
    done = re.match(r"(v\d+) request completed", msg)
    if http and done:
        return LogEvent(ts, f"{service}.request {http[1]} /{done[1]}", method=http[1], path=f"/{done[1]}", http_status=200,
                        message="request completed")
    ok = re.match(r"Scrape via (\w+) deemed successful", msg)
    if ok:
        return LogEvent(ts, f"{service}.scrape [{ok[1]}]", attrs={"firecrawl.engine": ok[1]}, message=scrub(msg))
    if lvl in ("error", "fatal"):
        low = msg.lower()
        if "anti-bot" in low:
            cls = f"{service}_search_blocked"
        elif "timeout" in low or "timed out" in low:
            cls = f"{service}_timeout"
        elif "search" in ctx.lower() or "search" in low:
            cls = f"{service}_search_error"
        elif "scrape" in ctx.lower() or "scrape" in low:
            cls = f"{service}_scrape_error"
        else:
            cls = f"{service}_error"
        return LogEvent(ts, f"{service}.error [{ctx or 'api'}]", status="ERROR", error_class=cls, message=scrub(msg)[:500])
    if lvl in ("warn", "warning") and re.search(r"retry|anti-bot|timeout|timed out|rate", msg, re.I):
        return LogEvent(ts, f"{service}.warning [{ctx or 'api'}]", status="UNSET", error_class=f"{service}_warning",
                        message=scrub(msg)[:500])
    return None


SEARX_RE = re.compile(r"^(?P<d>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(?P<ms>\d+)\s+(?P<lvl>[A-Z]+):(?P<logger>[^:]+):\s*(?P<msg>.*)$", re.S)
ACCESS_RE = re.compile(r"\"(?P<method>GET|POST|PUT|DELETE|HEAD) (?P<path>\S+) HTTP/[\d.]+\" (?P<status>\d{3})")


def parse_searxng(service: str, ts: datetime, body: str) -> Optional[LogEvent]:
    body = ANSI_RE.sub("", body)
    a = ACCESS_RE.search(body)
    if a:
        status = int(a["status"])
        path = a["path"].split("?")[0]
        return LogEvent(ts, f"{service}.request {a['method']} {path} ({status})", status="ERROR" if status >= 400 else "OK",
                        method=a["method"], path=path, http_status=status,
                        error_class=f"{service}_http_{status // 100}xx" if status >= 400 else None)
    m = SEARX_RE.match(body)
    if not m or m["lvl"] not in ("WARNING", "ERROR", "CRITICAL"):
        return None
    logger, msg = m["logger"], m["msg"]
    engine = logger.rsplit(".", 1)[-1].strip()
    if "TooManyRequests" in msg:
        cls = f"{service}_engine_rate_limited"
    elif re.search(r"Timeout|timed out", msg, re.I):
        cls = f"{service}_engine_timeout"
    elif re.search(r"Captcha|Access denied|Forbidden", msg, re.I):
        cls = f"{service}_engine_blocked"
    else:
        code = re.search(r"HTTPError.*?\('(\d{3})'", msg)
        if code:
            cls = f"{service}_engine_http_{code[1]}"
        elif "HTTP Request failed" in msg:
            cls = f"{service}_network_error"
        else:
            cls = f"{service}_engine_error"
    first = scrub(msg.splitlines()[0])[:400]
    return LogEvent(ts, f"{service}.engine_error [{engine}]", status="ERROR", error_class=cls, message=first,
                    attrs={"searxng.engine": engine, "searxng.level": m["lvl"]})


PROXY_RE = re.compile(
    r"^\[Proxy\]\s+(?P<t>\d\d:\d\d:\d\d)\s+(?P<ev>start|ok|fail|next)\s+(?P<id>\w+)\s+(?P<att>a\d+)\s+(?P<prov>\S+)\s+-\s+(?P<model>\S+)(?P<rest>.*)$")


def parse_freellmapi(service: str, ts: datetime, body: str) -> Optional[LogEvent]:
    body = ANSI_RE.sub("", body)
    m = PROXY_RE.match(body)
    if m:
        rest = m["rest"]
        lat = re.search(r"lat=(\d+)ms", rest)
        dur = float(lat[1]) if lat else None
        base = {"llm.provider": m["prov"], "llm.model_name": m["model"], "freellmapi.attempt": m["att"],
                "freellmapi.request_id": m["id"]}
        if m["ev"] == "ok":
            i, o = re.search(r"in=(\d+)", rest), re.search(r"out=(\d+)", rest)
            attrs = {**base, "llm.token_count.prompt": int(i[1]) if i else 0, "llm.token_count.completion": int(o[1]) if o else 0}
            return LogEvent(ts, f"{service}.completion [{m['prov']}/{m['model']}]", span_kind="LLM", http_status=200,
                            duration_ms=dur, attrs=attrs)
        if m["ev"] == "fail":
            err = re.search(r'err="(.*)', rest)
            msg = scrub(err[1].rstrip('"') if err else rest)[:500]
            code = re.search(r"error (\d{3})", msg)
            cls = (f"{service}_rate_limited" if code and code[1] == "429"
                   else f"{service}_upstream_{code[1]}" if code else f"{service}_upstream_error")
            return LogEvent(ts, f"{service}.completion_failed [{m['prov']}/{m['model']}]", status="ERROR", span_kind="LLM",
                            http_status=int(code[1]) if code else 502, duration_ms=dur, error_class=cls, message=msg, attrs=base)
        return None
    if body.startswith("[FallbackLoop]"):
        if "exhausted" in body:
            return LogEvent(ts, f"{service}.routing_exhausted", status="ERROR", error_class=f"{service}_routing_exhausted",
                            message=scrub(body.splitlines()[0])[:400])
        if "client disconnected" in body:
            return LogEvent(ts, f"{service}.client_disconnected", status="UNSET", error_class=f"{service}_client_disconnected",
                            message=scrub(body.splitlines()[0])[:400])
    return None


ERROR_LINE_RE = re.compile(r"\b(ERROR|CRITICAL|FATAL|Traceback|Exception)\b")


def make_generic_parser(log_format: Optional[str]) -> Parser:
    """Parser from a user regex with named groups ts, method, path, status, duration_ms; else error-line detection."""
    rx = re.compile(log_format) if log_format else None

    def parse(service: str, ts: datetime, body: str) -> Optional[LogEvent]:
        body = ANSI_RE.sub("", body)
        if rx:
            m = rx.search(body)
            if not m:
                return None
            g = m.groupdict()
            status = int(g["status"]) if g.get("status") and str(g["status"]).isdigit() else None
            err = status is not None and status >= 400
            when = ts
            if g.get("ts"):
                when = ensure_utc(g["ts"])
            return LogEvent(when, f"{service}.request {g.get('method') or ''} {(g.get('path') or '').split('?')[0]}".strip()
                            + (f" ({status})" if status else ""),
                            status="ERROR" if err else "OK", method=g.get("method") or "",
                            path=(g.get("path") or "").split("?")[0], http_status=status,
                            duration_ms=float(g["duration_ms"]) if g.get("duration_ms") else None,
                            error_class=f"{service}_http_{status // 100}xx" if err else None)
        if ERROR_LINE_RE.search(body):
            return LogEvent(ts, f"{service}.log_error", status="ERROR", error_class=f"{service}_log_error", message=scrub(body)[:400])
        return None

    return parse


PARSERS: dict[str, Parser] = {"firecrawl": parse_firecrawl, "searxng": parse_searxng, "freellmapi": parse_freellmapi}


# ---------------------------------------------------------------- adapter


@dataclass
class ServiceSpec:
    name: str
    kind: str = "service"
    health_url: str = ""
    docker_containers: list[str] = field(default_factory=list)  # logs are parsed and state is inspected
    components: dict[str, str] = field(default_factory=dict)  # label -> container; state is inspected only
    log_path: str = ""
    process_name: str = ""
    log_format: str = ""
    parser: str = ""  # builtin parser name: firecrawl | searxng | freellmapi
    probe_interval: float = DEFAULT_PROBE_INTERVAL
    timeout: float = 3.0

    @classmethod
    def from_dict(cls, d: dict[str, Any], base: Optional["ServiceSpec"] = None) -> "ServiceSpec":
        spec = ServiceSpec(**{**(base.__dict__ if base else {}), "name": str(d["name"])})
        spec.docker_containers = list(spec.docker_containers)
        spec.components = dict(spec.components)
        if "kind" in d:
            spec.kind = str(d["kind"])
        for key in ("health_url", "log_path", "process_name", "log_format", "parser"):
            if key in d:
                setattr(spec, key, str(d[key]))
        dc = d.get("docker_container", d.get("docker_containers"))
        if dc is not None:
            spec.docker_containers = [dc] if isinstance(dc, str) else [str(x) for x in dc]
        comp = d.get("components")
        if isinstance(comp, dict):
            spec.components = {str(k): str(v) for k, v in comp.items()}
        elif isinstance(comp, list):
            spec.components = {str(c): str(c) for c in comp}
        if "probe_interval" in d:
            spec.probe_interval = float(d["probe_interval"])
        if "timeout" in d:
            spec.timeout = float(d["timeout"])
        return spec

    def validate(self) -> list[str]:
        errors = []
        if not self.name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.name):
            errors.append("name must use letters, digits, dot, dash or underscore")
        if self.kind not in SERVICE_KINDS:
            errors.append(f"kind must be one of {', '.join(SERVICE_KINDS)}")
        if not (self.health_url or self.docker_containers or self.log_path or self.process_name):
            errors.append("set at least one of health_url, docker_container, log_path or process_name")
        if self.health_url and urlsplit(self.health_url).scheme not in ("http", "https"):
            errors.append("health_url must start with http:// or https://")
        if self.log_format:
            try:
                re.compile(self.log_format)
            except re.error as e:
                errors.append(f"log_format is not a valid regex: {e}")
        return errors


BUILTIN_SPECS: dict[str, ServiceSpec] = {
    "everos": ServiceSpec("everos", "service", health_url="http://127.0.0.1:8000/health"),
    "searxng": ServiceSpec("searxng", "service", health_url="http://127.0.0.1:8888/healthz", docker_containers=["searxng"],
                           parser="searxng"),
    "freellmapi": ServiceSpec("freellmapi", "gateway", health_url="http://127.0.0.1:31415/health",
                              docker_containers=["freellmapi-freellmapi-1"], parser="freellmapi"),
    "firecrawl": ServiceSpec("firecrawl", "service", health_url="http://127.0.0.1:3003/", docker_containers=["firecrawl-api-1"],
                             components={"playwright": "firecrawl-playwright-service-1", "postgres": "firecrawl-nuq-postgres-1",
                                         "redis": "firecrawl-redis-1", "rabbitmq": "firecrawl-rabbitmq-1"},
                             parser="firecrawl"),
}


class ServiceAdapter:
    """Monitors one service. Works for built-ins and for [[services]] entries in config.toml."""

    def __init__(self, spec: ServiceSpec | dict[str, Any]) -> None:
        self.spec = spec if isinstance(spec, ServiceSpec) else ServiceSpec.from_dict(spec, BUILTIN_SPECS.get(str(spec.get("name"))))
        self.name = self.spec.name
        self.kind = self.spec.kind
        self._parser: Parser = PARSERS.get(self.spec.parser) or make_generic_parser(self.spec.log_format or None)
        self._explicit = False  # set by load_service_adapters when the user configured this service

    # -- discovery
    def discover(self, ctx: Context) -> bool:
        cfg = ctx.config
        if self._explicit or (cfg.enabled_services is not None and self.name in cfg.enabled_services):
            return True  # a configured service is polled even while down, so outages are recorded
        s = self.spec
        if s.health_url and http_probe(s.health_url, 1.0)[2] is None:
            return True
        if s.docker_containers and docker_available() and docker_state(s.docker_containers[0]):
            return True
        if s.process_name and process_running(s.process_name):
            return True
        return bool(s.log_path and Path(s.log_path).expanduser().is_file())

    # -- probing
    def probe(self) -> ProbeResult:
        s = self.spec
        if s.health_url:
            code, lat, err, body = http_probe(s.health_url, s.timeout)
            if err == "timeout":
                return ProbeResult("down", None, lat, f"{self.name}_timeout", "health check timed out")
            if err:
                return ProbeResult("down", None, lat, f"{self.name}_down", f"unreachable: {body}")
            extra: dict[str, Any] = {}
            degraded = None
            try:
                data = json.loads(body)
            except ValueError:
                data = None
            if isinstance(data, dict):
                if "version" in data:
                    extra[f"{self.name}.version"] = str(data["version"])[:40]
                st = data.get("status")
                if isinstance(st, str) and st.lower() not in ("ok", "healthy", "up", "ready", "running"):
                    degraded = f"status={st}"
                casc = data.get("cascade")
                if isinstance(casc, dict):
                    extra[f"{self.name}.cascade_healthy"] = bool(casc.get("healthy", True))
                    for k in ("pending", "failed_permanent", "failed_retryable"):
                        if isinstance(casc.get(k), int):
                            extra[f"{self.name}.{k}"] = casc[k]
                    if casc.get("healthy") is False:
                        degraded = degraded or "cascade unhealthy"
            if code is not None and code >= 500:
                return ProbeResult("degraded", code, lat, f"{self.name}_http_5xx", f"HTTP {code}", extra)
            if code is not None and code >= 400:
                return ProbeResult("degraded", code, lat, f"{self.name}_http_4xx", f"HTTP {code}", extra)
            if degraded:
                return ProbeResult("degraded", code, lat, f"{self.name}_degraded", degraded, extra)
            return ProbeResult("healthy", code, lat, None, "", extra)
        if s.docker_containers:
            st = docker_state(s.docker_containers[0])
            if st is None:
                return ProbeResult("down", None, 0.0, f"{self.name}_container_missing", "container not found")
            return _container_result(self.name, st)
        if s.process_name:
            ok = process_running(s.process_name)
            return ProbeResult("healthy" if ok else "down", None, 0.0, None if ok else f"{self.name}_down",
                               "" if ok else f"process {s.process_name} not running")
        p = Path(s.log_path).expanduser()
        ok = p.is_file()
        return ProbeResult("healthy" if ok else "down", None, 0.0, None if ok else f"{self.name}_down",
                           "" if ok else "log file missing")

    # -- polling
    def poll(self, ctx: Context, state: StateStore, store: SpanStore, cutoff_ts: float) -> PollResult:
        spans = errors = 0
        now = time.time()
        wm_key = safe_endpoint(self.spec.health_url) or ",".join(self.spec.docker_containers) or self.spec.log_path or self.name
        _, last = state.get_db_watermark(f"service:{self.name}", wm_key, "probe")
        last_probe = float(last.split("|")[0]) if last and "|" in last else 0.0
        if now - last_probe >= self.spec.probe_interval:
            res = self.probe()
            state.set_db_watermark(f"service:{self.name}", wm_key, "probe", last_ts=f"{now}|{res.status}")
            store.add(self._probe_span(self.name, res, now))
            spans += 1
            errors += res.status != "healthy"
            for label, container in self.spec.components.items():
                st = docker_state(container)
                cres = _container_result(f"{self.name}.{label}", st) if st else ProbeResult(
                    "down", None, 0.0, f"{self.name}_{label}_missing", "container not found")
                store.add(self._probe_span(f"{self.name}.{label}", cres, now, component=label, container=container))
                spans += 1
                errors += cres.status != "healthy"
        s_, e_ = self._poll_logs(state, store, cutoff_ts)
        return {self.name: (spans + s_, errors + e_)}

    def _probe_span(self, label: str, res: ProbeResult, now: float, component: str = "", container: str = ""):
        end = datetime.fromtimestamp(now, tz=timezone.utc)
        start = end - timedelta(milliseconds=max(res.latency_ms, 1.0))
        attrs = {
            "session.id": f"service:{self.name}", "harness": self.name, "service.name": self.name, "service.kind": self.kind,
            "service.status": res.status, "service.latency_ms": round(res.latency_ms, 1),
            "service.endpoint": safe_endpoint(self.spec.health_url) or container or self.spec.process_name or self.spec.log_path,
            "http.status_code": res.http_status, "error.class": res.error_class, **res.extra,
        }
        if component:
            attrs["service.component"] = component
        return build_span(
            trace_id=make_trace_id(self.name, "probes"), span_id=make_span_id("svc_probe", label, int(now)),
            name=f"{label}.probe [{res.status}]", span_kind="SERVICE", start_time=start, end_time=end,
            status_code="OK" if res.status == "healthy" else "ERROR", status_message=res.message, attributes=attrs)

    def _event_span(self, ev: LogEvent, key: str):
        end = ensure_utc(ev.ts)
        start = end - timedelta(milliseconds=max(ev.duration_ms or 1.0, 1.0))
        attrs: dict[str, Any] = {
            "session.id": f"service:{self.name}", "harness": self.name, "service.name": self.name, "service.kind": self.kind,
            "http.method": ev.method or None, "http.path": ev.path or None, "http.status_code": ev.http_status,
            "service.latency_ms": ev.duration_ms, "error.class": ev.error_class, **ev.attrs,
        }
        return build_span(
            trace_id=make_trace_id(self.name, "requests"), span_id=make_span_id("svc_event", self.name, key),
            name=ev.name, span_kind=ev.span_kind, start_time=start, end_time=end, status_code=ev.status,
            status_message=ev.message if ev.status != "OK" else "", attributes=attrs)

    def _poll_logs(self, state: StateStore, store: SpanStore, cutoff_ts: float) -> tuple[int, int]:
        spans = errors = 0
        s = self.spec
        if s.docker_containers and docker_available():
            for container in s.docker_containers:
                skip, last_ts = state.get_db_watermark(f"service_logs:{self.name}", container, "docker_logs")
                skip = int(skip or 0)  # lines already handled at the watermark timestamp
                since = last_ts or str(int(max(cutoff_ts, time.time() - 48 * 3600)))
                newest, at_newest = last_ts or "", skip
                seen_at: dict[str, int] = {}
                for line in docker_logs(container, since):
                    m = DOCKER_TS_RE.match(line)
                    if not m:
                        continue
                    ts_s, body = m.group(1), m.group(2)
                    if last_ts and ts_s < last_ts:
                        continue
                    seen_at[ts_s] = seen_at.get(ts_s, 0) + 1
                    if ts_s == last_ts and seen_at[ts_s] <= skip:
                        continue
                    if ts_s > newest:
                        newest = ts_s
                    ev = self._parser(self.name, ensure_utc(ts_s), body)
                    if ev is None:
                        continue
                    key = hashlib.sha256(f"{container}|{ts_s}|{body}".encode("utf-8", "replace")).hexdigest()[:16]
                    store.add(self._event_span(ev, key))
                    spans += 1
                    errors += ev.status == "ERROR"
                if newest:
                    state.set_db_watermark(f"service_logs:{self.name}", container, "docker_logs",
                                           last_rowid=seen_at.get(newest, at_newest), last_ts=newest)
        if s.log_path:
            sp, er = self._poll_file(state, store, Path(s.log_path).expanduser())
            spans, errors = spans + sp, errors + er
        return spans, errors

    def _poll_file(self, state: StateStore, store: SpanStore, path: Path) -> tuple[int, int]:
        if not path.is_file():
            return 0, 0
        st = path.stat()
        offset, _, ino = state.get_file_watermark(path)
        if ino is not None and (st.st_size < offset):
            offset = 0  # rotated or truncated
        spans = errors = 0
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(8 * 1024 * 1024)
        end = data.rfind(b"\n") + 1
        for i, raw in enumerate(data[:end].decode("utf-8", "replace").splitlines()):
            ev = self._parser(self.name, datetime.fromtimestamp(st.st_mtime, tz=timezone.utc), raw)
            if ev is None:
                continue
            store.add(self._event_span(ev, hashlib.sha256(f"{path}|{offset}|{i}|{raw}".encode("utf-8", "replace")).hexdigest()[:16]))
            spans += 1
            errors += ev.status == "ERROR"
        state.set_file_watermark(f"service:{self.name}", path, offset + end, st.st_mtime_ns, 1)
        return spans, errors


def _container_result(label: str, st: dict[str, str]) -> ProbeResult:
    if st["status"] != "running":
        return ProbeResult("down", None, 0.0, f"{label.replace('.', '_')}_container_down", f"container is {st['status']}")
    if st["health"] == "unhealthy":
        return ProbeResult("degraded", None, 0.0, f"{label.replace('.', '_')}_unhealthy", "container health check failing")
    return ProbeResult("healthy", None, 0.0, None, "")


def _builtin(name: str) -> Callable[[], ServiceAdapter]:
    return lambda: ServiceAdapter(BUILTIN_SPECS[name])


BUILTIN_SERVICE_ADAPTERS: dict[str, Callable[[], ServiceAdapter]] = {n: _builtin(n) for n in BUILTIN_SPECS}


def load_service_adapters(config: Config) -> list[ServiceAdapter]:
    """Adapters for [[services]] entries. An entry named like a built-in overrides that built-in's defaults."""
    out: list[ServiceAdapter] = []
    for entry in config.services:
        if not entry.get("name"):
            continue
        spec = ServiceSpec.from_dict(entry, BUILTIN_SPECS.get(str(entry["name"])))
        if spec.validate():
            continue
        ad = ServiceAdapter(spec)
        ad._explicit = True
        out.append(ad)
    return out


def validate_services_file(path: Path) -> list[str]:
    """Validate the [[services]] entries of a standalone TOML file."""
    import tomllib

    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        return [str(e)]
    entries = data.get("services")
    if not isinstance(entries, list) or not entries:
        return ["no [[services]] entries found"]
    errors: list[str] = []
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or not entry.get("name"):
            errors.append(f"[[services]] entry {i} has no name")
            continue
        errors += [f"{entry['name']}: {e}" for e in ServiceSpec.from_dict(entry, BUILTIN_SPECS.get(str(entry["name"]))).validate()]
    return errors
