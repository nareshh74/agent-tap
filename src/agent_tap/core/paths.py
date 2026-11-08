"""Cross-platform discovery of agent harness data directories."""
from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir, user_state_dir

APP = "agent-tap"


def _env_path(name: str) -> Path | None:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else None


def config_file() -> Path:
    return _env_path("AGENT_TAP_CONFIG") or Path(user_config_dir(APP, appauthor=False)) / "config.toml"


def adapters_dir() -> Path:
    return Path(user_config_dir(APP, appauthor=False)) / "adapters"


def default_spans_db() -> Path:
    return Path(user_data_dir(APP, appauthor=False)) / "spans.db"


def default_state_db() -> Path:
    return Path(user_state_dir(APP, appauthor=False)) / "state.sqlite"


def _existing(cands: list[Path | None]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for c in cands:
        if c is not None and c not in seen and c.exists():
            seen.add(c)
            out.append(c)
    return out


def claude_roots() -> list[Path]:
    xdg = _env_path("XDG_CONFIG_HOME")
    return _existing([Path.home() / ".claude" / "projects", xdg / "claude" / "projects" if xdg else None])


def codex_root() -> Path:
    return _env_path("CODEX_HOME") or Path.home() / ".codex"


def copilot_root() -> Path:
    return Path.home() / ".copilot"


def raven_root() -> Path:
    return Path.home() / ".raven"


def hermes_roots() -> list[Path]:
    local = _env_path("LOCALAPPDATA")
    xdg = _env_path("XDG_DATA_HOME")
    return _existing(
        [_env_path("HERMES_HOME"), Path.home() / ".hermes", local / "hermes" if local else None, xdg / "hermes" if xdg else None]
    )


def hermes_profiles(roots: list[Path] | None = None) -> list[tuple[str, Path]]:
    """Return (profile_name, dir): the root itself if it has state.db, plus every profiles/* directory."""
    out: list[tuple[str, Path]] = []
    for root in roots if roots is not None else hermes_roots():
        if (root / "state.db").exists():
            out.append(("default", root))
        pdir = root / "profiles"
        if pdir.is_dir():
            out.extend((p.name, p) for p in sorted(pdir.iterdir()) if p.is_dir())
    return out


def wsl_omniroute_dirs() -> list[Path]:
    """Opt-in (Windows only): locate OmniRoute docker volumes through the WSL2 UNC share."""
    if os.name != "nt":
        return []
    cands: list[Path | None] = []
    try:
        for distro in Path("//wsl.localhost").iterdir():
            cands.append(distro / "mnt" / "docker-desktop-disk" / "data" / "docker" / "volumes" / "omniroute-data-live" / "_data")
    except OSError:
        pass
    return _existing(cands)


def omniroute_dirs(wsl: bool = False) -> list[Path]:
    found = _existing([_env_path("AGENT_TAP_OMNIROUTE_DIR"), Path.home() / ".omniroute"])
    return found + (wsl_omniroute_dirs() if wsl else [])
