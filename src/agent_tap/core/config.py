"""Configuration: CLI overrides > AGENT_TAP_* env > config.toml > OS defaults."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths

_TRUE = {"1", "true", "yes", "on"}
_PATH_KEYS = ("spans_db", "state_db", "adapters_dir")


@dataclass
class Config:
    spans_db: Path = field(default_factory=paths.default_spans_db)
    state_db: Path = field(default_factory=paths.default_state_db)
    store_prompt_text: bool = False
    host: str = "127.0.0.1"
    port: int = 6007
    interval: float = 5.0
    lookback_hours: float = 48.0
    retention_days: int = 90
    project_name: str = "agent-analytics"
    enabled_harnesses: list[str] | None = None  # None = all discovered adapters
    adapters_dir: Path = field(default_factory=paths.adapters_dir)
    wsl_discovery: bool = False
    harness_dirs: dict[str, Path] = field(default_factory=dict)
    source: Path | None = None

    def harness_dir(self, name: str) -> Path | None:
        """Explicit data-dir override: AGENT_TAP_<NAME>_DIR env, then config.toml [paths]."""
        env = os.environ.get(f"AGENT_TAP_{name.upper().replace('-', '_')}_DIR")
        if env:
            return Path(env).expanduser()
        return self.harness_dirs.get(name)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {k: (str(v) if isinstance(v, Path) else v) for k, v in self.__dict__.items()}
        d["harness_dirs"] = {k: str(v) for k, v in self.harness_dirs.items()}
        return d


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    cfg = Config()
    f = Path(path) if path else paths.config_file()
    if f.is_file():
        data = tomllib.loads(f.read_text(encoding="utf-8"))
        cfg.source = f
        for k in _PATH_KEYS:
            if k in data:
                setattr(cfg, k, Path(data[k]).expanduser())
        for k in ("store_prompt_text", "host", "port", "interval", "lookback_hours", "retention_days",
                  "project_name", "enabled_harnesses", "wsl_discovery"):
            if k in data:
                setattr(cfg, k, data[k])
        if "enabled_harnesses" not in data and "adapters" in data:  # legacy key
            cfg.enabled_harnesses = data["adapters"]
        cfg.harness_dirs = {k: Path(v).expanduser() for k, v in (data.get("paths") or {}).items()}
    env = os.environ.get
    if env("AGENT_TAP_SPANS_DB"):
        cfg.spans_db = Path(env("AGENT_TAP_SPANS_DB")).expanduser()
    if env("AGENT_TAP_STATE_DB"):
        cfg.state_db = Path(env("AGENT_TAP_STATE_DB")).expanduser()
    if env("AGENT_TAP_STORE_PROMPTS") is not None:
        cfg.store_prompt_text = env("AGENT_TAP_STORE_PROMPTS", "").strip().lower() in _TRUE
    if env("AGENT_TAP_HOST"):
        cfg.host = env("AGENT_TAP_HOST")
    if env("AGENT_TAP_PORT"):
        cfg.port = int(env("AGENT_TAP_PORT"))
    if env("AGENT_TAP_WSL") is not None:
        cfg.wsl_discovery = env("AGENT_TAP_WSL", "").strip().lower() in _TRUE
    for k, v in (overrides or {}).items():
        if v is not None and hasattr(cfg, k):
            setattr(cfg, k, Path(v) if k in _PATH_KEYS else v)
    return cfg
