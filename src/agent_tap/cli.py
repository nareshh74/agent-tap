"""agent-tap command line interface."""
from __future__ import annotations

import argparse
import json
import platform as _platform
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .core import paths
from .core.config import Config, load_config

QUERY_PRESETS = {
    "harnesses": (
        "SELECT COALESCE(json_extract(attributes,'$.harness'),'unknown') AS harness, COUNT(*) AS spans, "
        "SUM(status_code='ERROR') AS errors, SUM(COALESCE(llm_token_count_prompt,0)) AS prompt_tokens, "
        "SUM(COALESCE(llm_token_count_completion,0)) AS completion_tokens FROM spans GROUP BY 1 ORDER BY spans DESC"
    ),
    "errors": (
        "SELECT start_time, COALESCE(json_extract(attributes,'$.harness'),'unknown') AS harness, name, status_message "
        "FROM spans WHERE status_code='ERROR' ORDER BY start_time DESC LIMIT 50"
    ),
    "tools": (
        "SELECT json_extract(attributes,'$.tool.name') AS tool, COUNT(*) AS calls, SUM(status_code='ERROR') AS errors "
        "FROM spans WHERE span_kind='TOOL' GROUP BY 1 ORDER BY calls DESC LIMIT 50"
    ),
    "sessions": (
        "SELECT session_id, start_time, end_time FROM project_sessions ORDER BY end_time DESC LIMIT 50"
    ),
}


def _build_config(args: argparse.Namespace) -> Config:
    return load_config(
        getattr(args, "config", None),
        {
            "spans_db": getattr(args, "spans_db", None),
            "state_db": getattr(args, "state_db", None),
            "host": getattr(args, "host", None),
            "port": getattr(args, "port", None),
        },
    )


def _split(v: str | None) -> list[str] | None:
    return [h.strip() for h in v.split(",") if h.strip()] if v else None


def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    from .onboarding import can_prompt, run_init

    interactive = not (args.yes or args.non_interactive) and can_prompt()
    try:
        run_init(cfg, harnesses=_split(args.harnesses), prompt_text=True if getattr(args, "store_prompts", False) else None, interactive=interactive)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def maybe_first_run_setup(cfg: Config, args: argparse.Namespace) -> Config:
    """Offer setup once when no config.toml exists and a human is at the terminal."""
    from .onboarding import can_prompt

    if cfg.source is not None or getattr(args, "non_interactive", False):
        return cfg
    if _split(getattr(args, "harnesses", None)) or (can_prompt() and not getattr(args, "yes", False)):
        rc = cmd_init(cfg, args)
        if rc:
            raise SystemExit(rc)
        return load_config(None, {k: getattr(args, k, None) for k in ("spans_db", "state_db", "host", "port")})
    return cfg


def run_once(adapters: list, ctx, state, store, cutoff_ts: float) -> dict[str, Any]:
    from .adapters.base import run_adapters

    return run_adapters(adapters, ctx, state, store, cutoff_ts)


def _setup_runtime(cfg: Config):
    from .adapters.base import Context, load_adapters
    from .emitter import SpanStore, set_store_prompt_text
    from .state import StateStore

    set_store_prompt_text(cfg.store_prompt_text)
    return load_adapters(cfg), Context(cfg), StateStore(cfg.state_db), SpanStore(cfg.spans_db, cfg.project_name)


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    from .digest import prune_spans

    adapters, ctx, state, store = _setup_runtime(cfg)
    hours = args.hours if args.hours is not None else cfg.lookback_hours
    if getattr(args, "reset_watermarks", False):
        state.conn.executescript(
            "DELETE FROM file_watermarks; DELETE FROM db_watermarks; DELETE FROM error_buckets; DELETE FROM source_health;"
        )
        state.commit()
    if args.once:
        res = run_once(adapters, ctx, state, store, time.time() - hours * 3600.0)
        state.close()
        print(json.dumps({"status": "ok", "total_sent": store.total_sent, "sources": res}, indent=2))
        return 0

    if not args.no_dashboard:
        from .dashboard import run_dashboard_server

        threading.Thread(
            target=run_dashboard_server, kwargs={"host": cfg.host, "port": cfg.port, "config": cfg}, daemon=True
        ).start()
    interval = args.interval if args.interval is not None else cfg.interval
    last_prune = 0.0
    try:
        while True:
            run_once(adapters, ctx, state, store, time.time() - hours * 3600.0)
            if time.time() - last_prune > 3600.0:
                try:
                    prune_spans(cfg.spans_db, cfg.retention_days)
                except Exception:
                    pass
                last_prune = time.time()
            time.sleep(max(interval, 1.0))
    except KeyboardInterrupt:
        return 0


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    from .dashboard import run_dashboard_server

    run_dashboard_server(host=cfg.host, port=cfg.port, config=cfg)
    return 0


def cmd_query(cfg: Config, args: argparse.Namespace) -> int:
    sql = QUERY_PRESETS.get(args.sql, args.sql)
    if not cfg.spans_db.exists():
        print(f"error: spans db not found: {cfg.spans_db}", file=sys.stderr)
        return 1
    con = sqlite3.connect(f"file:{cfg.spans_db.as_posix()}?mode=ro", uri=True, timeout=10.0)
    con.execute("PRAGMA query_only=ON")
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(sql).fetchall()]
    except sqlite3.Error as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        con.close()
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        for r in rows:
            print("\t".join(str(v) for v in r.values()))
    return 0


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    from .adapters.base import Context, load_adapters

    ctx = Context(cfg)
    report: dict[str, Any] = {
        "version": __version__,
        "python": sys.version.split()[0],
        "os": f"{_platform.system()} {_platform.release()}",
        "config_file": str(cfg.source or paths.config_file()) + ("" if cfg.source else " (not found; using defaults)"),
        "store_prompt_text": cfg.store_prompt_text,
        "spans_db": {"path": str(cfg.spans_db), "exists": cfg.spans_db.exists()},
        "state_db": {"path": str(cfg.state_db), "exists": cfg.state_db.exists()},
        "adapters": {},
    }
    for ad in load_adapters(cfg):
        try:
            report["adapters"][ad.name] = "found" if ad.discover(ctx) else "no data found"
        except Exception as e:
            report["adapters"][ad.name] = f"error: {e}"
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for k, v in report.items():
            if k == "adapters":
                print("adapters:")
                for n, s in v.items():
                    print(f"  {n}: {s}")
            else:
                print(f"{k}: {v}")
    return 0


def cmd_adapters(cfg: Config, args: argparse.Namespace) -> int:
    from .adapters.base import Context, load_adapters

    if args.action == "validate":
        import tomllib

        from .adapters.declarative import validate_file
        from .adapters.service import validate_services_file

        try:
            has_services = "services" in tomllib.loads(Path(args.file).read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            has_services = False
        errors = validate_services_file(Path(args.file)) if has_services else validate_file(Path(args.file))
        if errors:
            print("\n".join(f"error: {e}" for e in errors), file=sys.stderr)
            return 1
        print(f"ok: {args.file}")
        return 0
    ctx = Context(cfg)
    for ad in load_adapters(cfg):
        print(f"{ad.name}\t{'found' if ad.discover(ctx) else 'no data'}")
    return 0


def cmd_service(cfg: Config, args: argparse.Namespace) -> int:
    from .platform.service import service_main

    code, text = service_main(args.action, os_name=args.os, dry_run=args.dry_run)
    if text:
        print(text)
    return int(code)


def _setup_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--yes", "-y", action="store_true", help="Accept defaults without prompting")
    p.add_argument("--non-interactive", action="store_true", help="Never prompt (safe for CI/scripts/agents)")
    p.add_argument("--harnesses", help="Comma-separated harnesses to enable (skips the prompt)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-tap", description="Local-first session analytics for AI coding agents")
    parser.add_argument("--version", action="version", version=f"agent-tap {__version__}")
    parser.add_argument("--config", help="Path to config.toml (default: AGENT_TAP_CONFIG or the user config dir)")
    parser.add_argument("--spans-db", help="Override spans database path")
    parser.add_argument("--state-db", help="Override state database path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="Check config, databases and adapter discovery").add_argument("--json", action="store_true")

    for name in ("run", "daemon", "once"):
        p = sub.add_parser(name, help="Collect spans (daemon = run --watch, once = run --once)" if name == "run" else f"Alias for run ({name})")
        p.add_argument("--hours", type=float, default=None, help="Lookback window in hours")
        p.add_argument("--interval", type=float, default=None, help="Poll interval seconds (watch mode)")
        p.add_argument("--no-dashboard", action="store_true", help="Do not start the dashboard in watch mode")
        p.add_argument("--reset-watermarks", action="store_true")
        if name == "run":
            _setup_flags(p)
            g = p.add_mutually_exclusive_group()
            g.add_argument("--once", action="store_true")
            g.add_argument("--watch", action="store_true")

    for name in ("serve", "serve-dashboard"):
        p = sub.add_parser(name, help="Run the dashboard")
        _setup_flags(p)
        p.add_argument("--host")
        p.add_argument("--port", type=int)

    for name in ("init", "setup"):
        p = sub.add_parser(name, help="Interactive setup: choose harnesses, paths and prompt-text storage")
        _setup_flags(p)
        p.add_argument("--store-prompts", action="store_true", help="Opt in to storing prompt/tool text")

    p = sub.add_parser("query", help="Run read-only SQL or a preset (" + ", ".join(QUERY_PRESETS) + ")")
    p.add_argument("sql")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("digest", help="Correlated session & error digest")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("prune", help="Delete spans older than N days")
    p.add_argument("--days", type=int, default=None)

    p = sub.add_parser("service", help="Install/manage the background service")
    p.add_argument("action", choices=["install", "status", "uninstall", "render"])
    p.add_argument("--os", choices=["windows", "macos", "linux"], default=None)
    p.add_argument("--dry-run", action="store_true")
    _setup_flags(p)

    p = sub.add_parser("adapters", help="List or validate adapters")
    p.add_argument("action", choices=["list", "validate"])
    p.add_argument("file", nargs="?")

    from . import bugreport
    from .onboard import add_parser as add_onboard_parser

    add_onboard_parser(sub)
    bugreport.add_parser(sub)

    p = sub.add_parser("config", help="Show effective configuration")
    p.add_argument("action", choices=["show"])

    args = parser.parse_args(argv)
    if args.cmd == "adapters" and args.action == "validate" and not args.file:
        parser.error("adapters validate requires a <file.toml>")
    cfg = _build_config(args)

    if args.cmd == "init" or args.cmd == "setup":
        return cmd_init(cfg, args)
    first_run_cmds = args.cmd in ("serve", "serve-dashboard") or (args.cmd == "run" and not args.once) or (
        args.cmd == "service" and args.action == "install" and not args.dry_run
    )
    if first_run_cmds:
        cfg = maybe_first_run_setup(cfg, args)

    if args.cmd in ("daemon", "once"):
        args.once = args.cmd == "once"
        args.watch = args.cmd == "daemon"
        args.cmd = "run"
    if args.cmd == "run":
        args.once = bool(args.once)
        return cmd_run(cfg, args)
    if args.cmd in ("serve", "serve-dashboard"):
        return cmd_serve(cfg, args)
    if args.cmd == "query":
        return cmd_query(cfg, args)
    if args.cmd == "doctor":
        return cmd_doctor(cfg, args)
    if args.cmd == "digest":
        from .digest import generate_digest

        print(generate_digest(hours=args.hours, phoenix_db=cfg.spans_db, state_db=cfg.state_db, as_json=args.json, service_kinds=cfg.service_kinds()))
        return 0
    if args.cmd == "prune":
        from .digest import prune_spans

        print(json.dumps(prune_spans(cfg.spans_db, args.days or cfg.retention_days), indent=2))
        return 0
    if args.cmd == "service":
        return cmd_service(cfg, args)
    if args.cmd == "adapters":
        return cmd_adapters(cfg, args)
    if args.cmd in ("onboard", "service-add"):
        from .onboard import cmd_onboard

        return cmd_onboard(cfg, args)
    if args.cmd == "bug-report":
        from . import bugreport

        return bugreport.cmd_bug_report(cfg, args)
    if args.cmd == "config":
        print(json.dumps(cfg.as_dict(), indent=2, default=str))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
