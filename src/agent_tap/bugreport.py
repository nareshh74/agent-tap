"""Collect a redacted diagnostic bundle for GitHub issues (`agent-tap bug-report`)."""
from __future__ import annotations

import getpass
import json
import os
import platform
import re
import sqlite3
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from . import __version__
from .core.config import Config
from .redact import redact_text

ISSUE_URL = "https://github.com/nareshh74/agent-tap/issues/new"
TEMPLATE = "bug_report.yml"
MAX_URL = 7000
TRUNCATION_NOTE = "\n[truncated: run `agent-tap bug-report` and paste the full output]"

SERVICE_OPTIONS = [
    "claude-code", "codex", "copilot-cli", "hermes", "raven", "omniroute", "everos", "searxng",
    "freellmapi", "firecrawl", "aider", "cline", "other service", "other harness",
]
OS_OPTIONS = ["Windows", "macOS", "Linux", "WSL2"]
INSTALL_OPTIONS = ["pipx", "uv tool", "pip", "from source", "other"]

_SECRET_KEY = re.compile(r"token|secret|passw|key|auth|cookie|credential|header", re.I)
_GENERIC_HOME = re.compile(
    r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/Users/|/home/)[^\\/\s'\"<>|:*?]+", re.I
)
_SECTION_CAP = 6000
_LOG_LINES = 40


def _home_names() -> tuple[list[str], list[str]]:
    homes: set[str] = set()
    for raw in (str(Path.home()), os.environ.get("USERPROFILE", ""), os.environ.get("HOME", "")):
        if raw and len(raw) > 3:
            homes.add(raw.rstrip("\\/"))
    users: set[str] = set()
    for fn in (getpass.getuser, os.getlogin):
        try:
            users.add(fn())
        except Exception:
            pass
    users.update(filter(None, (os.environ.get("USERNAME"), os.environ.get("USER"))))
    generic = {"user", "users", "admin", "root", "runner", "ubuntu", "default", "guest"}
    return sorted(homes, key=len, reverse=True), [u for u in users if len(u) >= 3 and u.lower() not in generic]


def scrub(text: Any) -> str:
    """Redact secrets, then replace the home directory and user name with `~`."""
    out = redact_text(str(text))
    homes, users = _home_names()
    for home in homes:
        variants = {home, home.replace("\\", "/"), home.replace("/", "\\"), home.replace("\\", "\\\\")}
        for v in sorted(variants, key=len, reverse=True):
            out = re.sub(re.escape(v), "~", out, flags=re.I)
    out = _GENERIC_HOME.sub("~", out)
    for user in users:
        out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(user)}(?![A-Za-z0-9])", "~", out, flags=re.I)
    return out


def _cap(text: str, limit: int = _SECTION_CAP) -> str:
    return text if len(text) <= limit else text[: limit - 15] + "\n[...truncated]"


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OSError):
        return None


def install_method() -> str:
    """Guess how agent-tap was installed without exposing any path."""
    prefix = sys.prefix.replace("\\", "/").lower()
    try:
        from importlib.metadata import distribution

        direct = distribution("agent-tap").read_text("direct_url.json")
        if direct:
            info = json.loads(direct)
            if (info.get("dir_info") or {}).get("editable") or str(info.get("url", "")).startswith("file:"):
                return "from source"
    except Exception:
        pass
    if "/pipx/" in prefix:
        return "pipx"
    if "/uv/tools/" in prefix or "/uv/tool/" in prefix:
        return "uv tool"
    return "pip"


def _os_label() -> str:
    system = platform.system()
    if system == "Windows":
        return "Windows"
    if system == "Darwin":
        return "macOS"
    if "microsoft" in platform.release().lower():
        return "WSL2"
    return "Linux" if system == "Linux" else system


def _services(cfg: Config) -> list[dict[str, str]]:
    from .adapters.base import Context, adapter_kind, load_adapters

    ctx, rows = Context(cfg), []
    try:
        adapters = load_adapters(cfg)
    except Exception as e:
        return [{"name": "(adapter loading failed)", "kind": "unknown", "status": scrub(f"error: {e}")}]
    for ad in adapters:
        try:
            status = "found" if ad.discover(ctx) else "no data found"
        except Exception as e:
            status = scrub(f"error: {type(e).__name__}: {e}")
        rows.append({"name": ad.name, "kind": adapter_kind(ad), "status": status})
    return rows


def _redact_spec(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "[REDACTED]" if _SECRET_KEY.search(str(k)) else _redact_spec(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_spec(v) for v in value]
    return scrub(value) if isinstance(value, str) else value


def _service_focus(cfg: Config, service: str) -> dict[str, Any]:
    focus: dict[str, Any] = {"name": service}
    spec = next((s for s in cfg.services if s.get("name") == service), None)
    if spec is not None:
        focus["spec"] = _redact_spec(spec)
    if not cfg.spans_db.exists():
        return focus
    try:
        con = sqlite3.connect(f"file:{cfg.spans_db.as_posix()}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT name, status_code, start_time, attributes FROM spans WHERE "
            "JSON_EXTRACT(attributes,'$.harness') = ? OR JSON_EXTRACT(attributes,'$.service.name') = ? "
            "ORDER BY start_time DESC LIMIT 1",
            (service, service),
        ).fetchone()
        con.close()
    except sqlite3.Error:
        return focus
    if row:
        try:
            attrs = json.loads(row["attributes"] or "{}")
        except ValueError:
            attrs = {}
        focus["latest_probe"] = {
            "span": scrub(row["name"]),
            "status_code": row["status_code"],
            "start_time": str(row["start_time"])[:19],
            "service_status": (attrs.get("service") or {}).get("status"),
            "http_status": (attrs.get("http") or {}).get("status_code"),
            "error_class": (attrs.get("error") or {}).get("class"),
        }
    return focus


def _source_health(cfg: Config) -> list[dict[str, Any]]:
    if not cfg.state_db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{cfg.state_db.as_posix()}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM source_health ORDER BY source").fetchall()
        con.close()
    except sqlite3.Error:
        return []
    return [
        {
            "source": r["source"],
            "last_poll_at": _iso(r["last_poll_at"]),
            "last_event_at": _iso(r["last_event_at"]),
            "last_error": _cap(scrub(r["last_error"]), 300) if r["last_error"] else None,
        }
        for r in rows
    ]


def _config_summary(cfg: Config) -> dict[str, Any]:
    return {
        "config_file_present": cfg.source is not None,
        "host": cfg.host,
        "port": cfg.port,
        "interval": cfg.interval,
        "lookback_hours": cfg.lookback_hours,
        "retention_days": cfg.retention_days,
        "store_prompt_text": cfg.store_prompt_text,
        "project_name": cfg.project_name,
        "wsl_discovery": cfg.wsl_discovery,
        "adapters_dir": cfg.adapters_dir.name,
        "enabled_services": cfg.enabled_services,
        "configured_services": sorted(cfg.service_kinds()),
    }


def _log_tail(cfg: Config) -> str:
    env = os.environ.get("AGENT_TAP_LOG")
    candidates = [Path(env)] if env else []
    candidates += [cfg.state_db.parent.parent / "logs" / "agent-tap.err", cfg.state_db.parent / "agent-tap.err"]
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size:
                with path.open("rb") as f:
                    f.seek(max(0, path.stat().st_size - 65536))
                    text = f.read().decode("utf-8", errors="replace")
                return _cap(scrub("\n".join(text.splitlines()[-_LOG_LINES:])))
        except OSError:
            continue
    return ""


def build_bundle(cfg: Config, *, error_text: str | None = None, service: str | None = None) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "agent_tap_version": __version__,
        "service": service,
        "environment": {
            "os": _os_label(),
            "platform": scrub(platform.platform()),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "install_method": install_method(),
        },
        "services": _services(cfg),
        "service_focus": _service_focus(cfg, service) if service else None,
        "source_health": _source_health(cfg),
        "config": _config_summary(cfg),
        "log_tail": _log_tail(cfg),
        "error_text": _cap(scrub(error_text)) if error_text else "",
    }


def render_markdown(bundle: dict[str, Any]) -> str:
    env = bundle["environment"]
    lines = [
        "### Environment",
        f"- agent-tap: {bundle['agent_tap_version']}",
        f"- OS: {env['os']} ({env['platform']}, {env['machine']})",
        f"- Python: {env['python']}",
        f"- Install method: {env['install_method']}",
        f"- Generated: {bundle['generated_at']}",
        "",
        "### Services",
    ]
    for s in bundle["services"]:
        lines.append(f"- {s['name']} ({s['kind']}): {s['status']}")
    focus = bundle.get("service_focus")
    if focus:
        lines += ["", f"Focus service: {focus['name']}", "```json", json.dumps({k: v for k, v in focus.items() if k != "name"}, indent=2, default=str), "```"]
    lines += ["", "### Source health"]
    if bundle["source_health"]:
        lines += ["| Source | Last poll | Last event | Last error |", "|---|---|---|---|"]
        for r in bundle["source_health"]:
            err = (r["last_error"] or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r['source']} | {r['last_poll_at'] or '-'} | {r['last_event_at'] or '-'} | {err or '-'} |")
    else:
        lines.append("No source health recorded yet.")
    lines += ["", "### Config", "```json", json.dumps(bundle["config"], indent=2, default=str), "```", "", "### Error output"]
    err = "\n\n".join(t for t in (bundle["error_text"], bundle["log_tail"]) if t)
    lines += ["```text", err or "(none supplied)", "```"]
    return "\n".join(lines) + "\n"


def _dropdown(value: str, options: list[str], fallback: str) -> str:
    return value if value in options else fallback


def issue_url(bundle: dict[str, Any]) -> str:
    """Prefilled new-issue URL; the diagnostics field is trimmed to keep the URL under MAX_URL characters."""
    env = bundle["environment"]
    service = bundle.get("service") or ""
    kinds = {s["name"]: s["kind"] for s in bundle["services"]}
    fallback = "other harness" if kinds.get(service, "harness") == "harness" else "other service"
    first = (bundle["error_text"].strip().splitlines() or [""])[0][:70]
    params = {
        "template": TEMPLATE,
        "title": f"[bug] {service or 'agent-tap'}: {first or 'describe the problem'}",
        "version": bundle["agent_tap_version"],
        "os": _dropdown(env["os"], OS_OPTIONS, "Linux"),
        "install_method": _dropdown(env["install_method"], INSTALL_OPTIONS, "other"),
        "service": _dropdown(service, SERVICE_OPTIONS, fallback),
    }
    diagnostics = render_markdown(bundle)

    def build(text: str) -> str:
        return ISSUE_URL + "?" + urlencode({**params, "diagnostics": text}, quote_via=quote)

    url = build(diagnostics)
    if len(url) <= MAX_URL:
        return url
    lo, hi = 0, len(diagnostics)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(build(diagnostics[:mid] + TRUNCATION_NOTE)) <= MAX_URL:
            lo = mid
        else:
            hi = mid - 1
    return build(diagnostics[:lo] + TRUNCATION_NOTE)


def cmd_bug_report(cfg: Config, args: Any) -> int:
    text = getattr(args, "error_text", None) or ""
    error_file = getattr(args, "error_file", None)
    if error_file:
        try:
            text = (text + "\n" if text else "") + Path(error_file).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"error: cannot read {error_file}: {e.strerror}", file=sys.stderr)
            return 2
    bundle = build_bundle(cfg, error_text=text or None, service=getattr(args, "service", None))
    output = json.dumps(bundle, indent=2, default=str) if getattr(args, "json", False) else render_markdown(bundle)
    out_path = getattr(args, "output", None)
    if out_path:
        Path(out_path).write_text(output, encoding="utf-8")
        print(f"Wrote {out_path}", file=sys.stderr)
    else:
        print(output)
    if getattr(args, "open", False):
        url = issue_url(bundle)
        opened = webbrowser.open(url)
        print(
            "Opened a prefilled GitHub issue in your browser." if opened else f"Open this URL to file the issue:\n{url}",
            file=sys.stderr,
        )
        print("The full bundle is above (or in the output file). Paste it into the Diagnostics field if the form shows a truncation note.", file=sys.stderr)
    return 0


def add_parser(sub: Any) -> Any:
    p = sub.add_parser("bug-report", help="Print a redacted diagnostic bundle for a GitHub issue")
    p.add_argument("--error-text", help="Error message or traceback to include")
    p.add_argument("--error-file", help="File that holds a traceback to include")
    p.add_argument("--service", help="Service or harness the problem is about")
    p.add_argument("--open", action="store_true", help="Open a prefilled GitHub issue in the browser")
    p.add_argument("--json", action="store_true", help="Print the bundle as JSON")
    p.add_argument("--output", help="Write the bundle to a file instead of stdout")
    return p
