"""Lightweight span model (no Phoenix/OpenTelemetry dependency)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class SpanKind(str, Enum):
    LLM = "LLM"
    CHAIN = "CHAIN"
    TOOL = "TOOL"
    AGENT = "AGENT"
    RETRIEVER = "RETRIEVER"
    EMBEDDING = "EMBEDDING"
    RERANKER = "RERANKER"
    EVALUATOR = "EVALUATOR"
    GUARDRAIL = "GUARDRAIL"
    SERVICE = "SERVICE"
    UNKNOWN = "UNKNOWN"


class SpanStatusCode(str, Enum):
    OK = "OK"
    ERROR = "ERROR"
    UNSET = "UNSET"


@dataclass(frozen=True)
class SpanContext:
    trace_id: str
    span_id: str


@dataclass
class SpanEvent:
    name: str
    timestamp: datetime
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class Span:
    name: str
    context: SpanContext
    span_kind: SpanKind
    parent_id: Optional[str]
    start_time: datetime
    end_time: datetime
    status_code: SpanStatusCode = SpanStatusCode.UNSET
    status_message: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[SpanEvent] = field(default_factory=list)
    conversation: Any = None


SERVICE_KINDS = ("harness", "gateway", "service")
# Built-in non-harness services. Anything not listed here or in config [[services]] is a harness.
BUILTIN_SERVICE_KINDS = {
    "omniroute": "gateway",
    "freellmapi": "gateway",
    "everos": "service",
    "searxng": "service",
    "firecrawl": "service",
}


def service_kind_for(name: str, configured: dict[str, str] | None = None) -> str:
    """Return "harness", "gateway" or "service" for a span's harness/service name."""
    if configured and name in configured:
        return configured[name]
    return BUILTIN_SERVICE_KINDS.get(name, "harness")
