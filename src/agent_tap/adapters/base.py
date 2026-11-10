"""Adapter contract and discovery (entry-point group ``agent_tap.harnesses`` + declarative TOML adapters)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from ..core.config import Config

if TYPE_CHECKING:
    from ..emitter import SpanStore
    from ..state import StateStore

ENTRY_POINT_GROUP = "agent_tap.harnesses"

# stage name -> (spans_emitted, errors_emitted)
PollResult = dict[str, tuple[int, int]]


@dataclass
class Context:
    config: Config

    def dir_for(self, name: str, default: Path | None = None) -> Path | None:
        """Config/env override for an adapter's data dir, else the OS-discovered default."""
        return self.config.harness_dir(name) or default


@runtime_checkable
class HarnessAdapter(Protocol):
    name: str
    kind: str  # "harness" (default when missing), "gateway" or "service"

    def discover(self, ctx: Context) -> bool:
        """True when this harness has data on this machine."""

    def poll(self, ctx: Context, state: "StateStore", store: "SpanStore", cutoff_ts: float) -> PollResult:
        """Emit spans newer than cutoff_ts into store; return per-stage (spans, errors)."""


def builtin_adapters() -> dict[str, Callable[[], HarnessAdapter]]:
    """Fallback registry so a source checkout works before entry points are (re)installed."""
    from .claude_code import ClaudeCodeAdapter
    from .codex import CodexAdapter
    from .copilot_cli import CopilotCliAdapter
    from .hermes import HermesAdapter
    from .omniroute import OmniRouteAdapter
    from .raven import RavenAdapter

    from .service import BUILTIN_SERVICE_ADAPTERS

    return {
        **BUILTIN_SERVICE_ADAPTERS,
        "raven": RavenAdapter, "hermes": HermesAdapter, "claude-code": ClaudeCodeAdapter,
        "codex": CodexAdapter, "copilot-cli": CopilotCliAdapter, "omniroute": OmniRouteAdapter,
    }


def adapter_kind(adapter: Any) -> str:
    from ..core.schema import SERVICE_KINDS, service_kind_for

    kind = getattr(adapter, "kind", None)
    return kind if kind in SERVICE_KINDS else service_kind_for(adapter.name)


def merge_service_adapters(adapters: list, extra: list) -> list:
    """Add service adapters; a configured service replaces an adapter with the same name."""
    names = {a.name for a in extra}
    return [a for a in adapters if a.name not in names] + list(extra)


def load_adapters(config: Config) -> list[HarnessAdapter]:
    """Built-ins + third-party entry points + declarative ``*.toml`` in config.adapters_dir."""
    factories: dict[str, Callable[[], HarnessAdapter]] = dict(builtin_adapters())
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            factories[ep.name] = ep.load()
        except Exception:
            continue
    adapters = [f() for f in factories.values()]
    from .declarative import load_declarative_adapters
    from .service import load_service_adapters

    adapters += load_declarative_adapters(config.adapters_dir)
    adapters = merge_service_adapters(adapters, load_service_adapters(config))
    if config.enabled_services is not None:
        adapters = [a for a in adapters if a.name in config.enabled_services]
    return adapters


def run_adapters(
    adapters: list[HarnessAdapter], ctx: Context, state: "StateStore", store: "SpanStore", cutoff_ts: float
) -> dict[str, dict[str, Any]]:
    """Poll every discovered adapter, isolating failures and recording per-stage source health."""
    results: dict[str, dict[str, Any]] = {}
    for ad in adapters:
        try:
            if not ad.discover(ctx):
                continue
            stages = ad.poll(ctx, state, store, cutoff_ts)
            store.flush()
            for stage, (sp, err) in stages.items():
                state.record_source_health(
                    stage, spans_emitted=sp, errors_emitted=err,
                    last_event_at=time.time() if sp > 0 else None, last_error=None,
                )
                results[stage] = {"spans": sp, "errors": err}
        except Exception as e:  # one broken adapter must not stop the others
            try:
                state.record_source_health(ad.name, last_error=str(e))
            except Exception:
                pass
            results[ad.name] = {"spans": 0, "errors": 0, "exception": str(e)}
        try:
            state.commit()
        except Exception:
            pass
    return results
