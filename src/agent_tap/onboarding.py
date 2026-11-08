"""Interactive first-run setup: pick harnesses, confirm paths, choose prompt-text storage."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any, Callable

from .adapters.base import Context, builtin_adapters
from .adapters.declarative import load_declarative_adapters
from .core import paths
from .core.config import Config


def examples_dir() -> Path:
    return Path(__file__).parent / "examples"


def detect_harnesses(cfg: Config) -> dict[str, bool]:
    """Map every known harness name to whether data exists on this machine."""
    ctx = Context(cfg)
    found: dict[str, bool] = {}
    for name, factory in builtin_adapters().items():
        try:
            found[name] = bool(factory().discover(ctx))
        except Exception:
            found[name] = False
    for ad in load_declarative_adapters(examples_dir()) + load_declarative_adapters(cfg.adapters_dir):
        try:
            found[ad.name] = bool(ad.discover(ctx))
        except Exception:
            found[ad.name] = False
    return found


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)  # JSON string escapes are valid TOML basic strings


def render_toml(data: dict[str, Any]) -> str:
    lines = [f"{k} = {_toml_value(v)}" for k, v in data.items() if not isinstance(v, dict)]
    for k, v in data.items():
        if isinstance(v, dict) and v:
            lines += ["", f"[{k}]"] + [f"{kk} = {_toml_value(vv)}" for kk, vv in v.items()]
    return "\n".join(lines) + "\n"


def _parse_selection(text: str, names: list[str], default: list[str]) -> list[str]:
    text = text.strip().lower()
    if not text:
        return default
    if text in ("a", "all"):
        return list(names)
    if text in ("n", "none"):
        return []
    chosen: list[str] = []
    for tok in text.replace(",", " ").split():
        if tok.isdigit() and 1 <= int(tok) <= len(names):
            chosen.append(names[int(tok) - 1])
        elif tok in names:
            chosen.append(tok)
        else:
            raise ValueError(f"unknown harness: {tok}")
    return list(dict.fromkeys(chosen))


def _yes(text: str, default: bool) -> bool:
    t = text.strip().lower()
    return default if not t else t in ("y", "yes", "true", "1")


def run_init(
    cfg: Config,
    *,
    config_path: Path | None = None,
    harnesses: list[str] | None = None,
    prompt_text: bool | None = None,
    interactive: bool = True,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> Path:
    """Write config.toml. Non-interactive when ``interactive`` is False (defaults: detected harnesses, prompts off)."""
    target = config_path or cfg.source or paths.config_file()
    existing: dict[str, Any] = {}
    if target.is_file():
        existing = tomllib.loads(target.read_text(encoding="utf-8"))
        shutil.copy2(target, target.with_name(target.name + ".bak"))
    data = dict(existing)
    path_overrides: dict[str, str] = dict(existing.get("paths") or {})

    detected = detect_harnesses(cfg)
    names = list(detected)
    default_sel = [n for n in names if detected[n]]
    if harnesses is not None:
        unknown = [h for h in harnesses if h not in names]
        if unknown:
            raise ValueError(f"unknown harness(es): {', '.join(unknown)}; known: {', '.join(names)}")
        selected = list(harnesses)
    elif interactive:
        out("Harnesses (data detected on this machine are marked *):")
        for i, n in enumerate(names, 1):
            out(f"  {i}. [{'*' if detected[n] else ' '}] {n}")
        while True:
            try:
                selected = _parse_selection(
                    ask("Enable which? numbers/names, 'all', 'none' [Enter = detected]: "), names, default_sel
                )
                break
            except ValueError as e:
                out(str(e))
    else:
        selected = default_sel
    data["enabled_harnesses"] = selected

    if interactive and harnesses is None:
        if "omniroute" in selected:
            p = ask("Custom OmniRoute data dir [Enter = auto-detect]: ").strip()
            if p:
                path_overrides["omniroute"] = p
        if os.name == "nt" and _yes(ask("Also scan WSL2 for OmniRoute data? [y/N]: "), False):
            data["wsl_discovery"] = True
        d = ask(f"Declarative adapter directory [Enter = {cfg.adapters_dir}]: ").strip()
        if d:
            data["adapters_dir"] = d
    if path_overrides:
        data["paths"] = path_overrides

    if prompt_text is None:
        prompt_text = (
            _yes(ask("Store full prompt/tool text locally? (stays on this machine) [y/N]: "), False)
            if interactive
            else bool(existing.get("store_prompt_text", False))
        )
    data["store_prompt_text"] = prompt_text

    adapters_dir = Path(data.get("adapters_dir", cfg.adapters_dir)).expanduser()
    for f in sorted(examples_dir().glob("*.toml")):
        if f.stem in selected and not (adapters_dir / f.name).exists():
            adapters_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, adapters_dir / f.name)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_toml(data), encoding="utf-8")
    out(f"Wrote {target} (enabled: {', '.join(selected) or 'none'}; store_prompt_text = {str(prompt_text).lower()})")
    return target


def can_prompt() -> bool:
    return sys.stdin is not None and sys.stdin.isatty() and sys.stdout.isatty()
