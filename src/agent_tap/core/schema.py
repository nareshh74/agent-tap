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
