"""`agent-tap onboard`: add any running service (HTTP, Docker, log file or process) to config.toml."""
from __future__ import annotations

import argparse
import shutil
import sys
import tomllib
from typing import Any

from .adapters.service import ServiceAdapter, ServiceSpec
from .core import paths
from .core.config import Config
from .core.schema import SERVICE_KINDS
from .onboarding import can_prompt, render_toml


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = sub.add_parser("onboard", aliases=["service-add"], help="Add a running service or harness to monitor")
    p.add_argument("--name", help="Service name, for example myapi")
    p.add_argument("--kind", choices=SERVICE_KINDS, default=None, help="harness, gateway or service (default: service)")
    p.add_argument("--health-url", help="HTTP endpoint that answers when the service is healthy")
    p.add_argument("--docker-container", action="append", default=None, help="Docker container to inspect and read logs from (repeatable)")
    p.add_argument("--log-path", help="Log file to read incrementally")
    p.add_argument("--process-name", help="Process image name to check, for example myapi.exe")
    p.add_argument("--log-format", help="Regex with named groups ts, method, path, status, duration_ms for request log lines")
    p.add_argument("--replace", action="store_true", help="Replace an existing entry with the same name")
    p.add_argument("--yes", "-y", action="store_true", help="Do not prompt")
    p.add_argument("--non-interactive", action="store_true", help="Never prompt (safe for scripts and agents)")
    return p


def _entry_from_args(args: argparse.Namespace) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": args.name}
    entry["kind"] = args.kind or "service"
    if args.health_url:
        entry["health_url"] = args.health_url
    if args.docker_container:
        entry["docker_container"] = args.docker_container[0] if len(args.docker_container) == 1 else list(args.docker_container)
    for key in ("log_path", "process_name", "log_format"):
        if getattr(args, key, None):
            entry[key] = getattr(args, key)
    return entry


def _prompt_missing(args: argparse.Namespace) -> None:
    def ask(q: str, default: str = "") -> str:
        return input(f"{q}{f' [{default}]' if default else ''}: ").strip() or default

    args.name = args.name or ask("Service name")
    args.kind = args.kind or ask("Kind (harness, gateway or service)", "service")
    if not (args.health_url or args.docker_container or args.log_path or args.process_name):
        args.health_url = ask("Health URL (Enter to skip)") or None
        dc = ask("Docker container (Enter to skip)")
        args.docker_container = [dc] if dc else None
        args.log_path = ask("Log file path (Enter to skip)") or None
        args.process_name = ask("Process name (Enter to skip)") or None


def cmd_onboard(cfg: Config, args: argparse.Namespace) -> int:
    interactive = not (args.yes or args.non_interactive) and can_prompt()
    if interactive and not args.name:
        _prompt_missing(args)
    if not args.name:
        print("error: --name is required (or run in a terminal for prompts)", file=sys.stderr)
        return 2
    entry = _entry_from_args(args)
    spec = ServiceSpec.from_dict(entry)
    errors = spec.validate()
    if errors:
        print("\n".join(f"error: {e}" for e in errors), file=sys.stderr)
        return 2

    target = cfg.source or paths.config_file()
    data: dict[str, Any] = {}
    if target.is_file():
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    services = [dict(s) for s in data.get("services") or []]
    existing = [i for i, s in enumerate(services) if s.get("name") == entry["name"]]
    if existing and not args.replace:
        print(f"error: service {entry['name']!r} is already configured; use --replace to overwrite it", file=sys.stderr)
        return 2

    res = ServiceAdapter(spec).probe()
    detail = f" ({res.error_class})" if res.error_class else ""
    print(f"Probe: {entry['name']} is {res.status}{detail}")

    if existing:
        services[existing[0]] = entry
    else:
        services.append(entry)
    data["services"] = services
    enabled = data.get("enabled_services") or data.get("enabled_harnesses") or data.get("adapters")
    if isinstance(enabled, list) and entry["name"] not in enabled:
        data.pop("enabled_harnesses", None)
        data.pop("adapters", None)
        data["enabled_services"] = [*enabled, entry["name"]]
    if target.is_file():
        shutil.copy2(target, target.with_name(target.name + ".bak"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_toml(data), encoding="utf-8")
    print(f"Wrote {target}. Run agent-tap doctor to confirm it is picked up.")
    return 0
