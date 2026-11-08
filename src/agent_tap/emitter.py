import json
from pathlib import Path
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .core.schema import Span, SpanContext, SpanEvent, SpanKind, SpanStatusCode

from .redact import redact_obj, redact_text

from .core.paths import default_spans_db

# Prompt/response bodies are dropped unless explicitly enabled (see docs/privacy.md).
STORE_PROMPT_TEXT = False
_PROMPT_KEYS = ("input.value", "output.value", "tool.parameters")
_PROMPT_PREFIXES = ("llm.input_messages", "llm.output_messages", "input.messages", "output.messages")


def set_store_prompt_text(enabled: bool) -> None:
    global STORE_PROMPT_TEXT
    STORE_PROMPT_TEXT = bool(enabled)


def _strip_prompts(attrs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        if k in _PROMPT_KEYS or k.startswith(_PROMPT_PREFIXES):
            if isinstance(v, str):
                out[k + ".length"] = len(v)
            continue
        out[k] = v
    return out


def ensure_utc(dt: datetime | float | int | str | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(dt, (int, float)):
        val = float(dt)
        if val > 1e14:  # nanoseconds
            val /= 1e9
        elif val > 1e11:  # milliseconds
            val /= 1e3
        return datetime.fromtimestamp(val, tz=timezone.utc)
    if isinstance(dt, str):
        s = dt.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(s)
            if parsed.tzinfo is None:
                ist = timezone(timedelta(hours=5, minutes=30))
                return parsed.replace(tzinfo=ist).astimezone(timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            try:
                return ensure_utc(float(s))
            except Exception:
                return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def _unflatten_attributes(flat: dict[str, Any], span_kind_str: str) -> dict[str, Any]:
    nested: dict[str, Any] = {}
    all_items = dict(flat)
    all_items.setdefault("openinference.span.kind", span_kind_str)
    for k, v in all_items.items():
        parts = [p for p in str(k).split(".") if p]
        if not parts:
            continue
        cur = nested
        for p in parts[:-1]:
            existing = cur.get(p)
            if not isinstance(existing, dict):
                cur[p] = {}
            cur = cur[p]
        cur[parts[-1]] = v
    return nested


def build_span(
    *,
    trace_id: str,
    span_id: str,
    name: str,
    span_kind: str = "CHAIN",
    parent_id: Optional[str] = None,
    start_time: Any = None,
    end_time: Any = None,
    status_code: str = "OK",
    status_message: str = "",
    attributes: Optional[dict[str, Any]] = None,
    events: Optional[list[dict[str, Any]]] = None,
) -> Span:
    st = ensure_utc(start_time)
    et = ensure_utc(end_time) if end_time is not None else st
    if et < st:
        et = st + timedelta(milliseconds=1)

    try:
        sk = SpanKind(span_kind.upper())
    except Exception:
        sk = SpanKind.UNKNOWN

    sc_upper = (status_code or "OK").upper()
    if sc_upper in ("ERROR", "FAILED", "FAIL"):
        sc = SpanStatusCode.ERROR
    elif sc_upper in ("OK", "COMPLETED", "SUCCESS"):
        sc = SpanStatusCode.OK
    else:
        sc = SpanStatusCode.UNSET

    clean_attrs: dict[str, Any] = {}
    if attributes:
        src = redact_obj(attributes)
        if not STORE_PROMPT_TEXT:
            src = _strip_prompts(src)
        for k, v in src.items():
            if v is None:
                continue
            if isinstance(v, (str, bool, int, float)):
                clean_attrs[str(k)] = v
            elif isinstance(v, (list, tuple)):
                clean_attrs[str(k)] = [str(x) if not isinstance(x, (str, bool, int, float)) else x for x in v]
            else:
                clean_attrs[str(k)] = json.dumps(v, ensure_ascii=False)

    span_events: list[SpanEvent] = []
    if events:
        for ev in events:
            ev_attrs = {}
            for k, v in redact_obj(ev.get("attributes") or {}).items():
                if v is not None:
                    ev_attrs[str(k)] = v if isinstance(v, (str, bool, int, float)) else str(v)
            span_events.append(
                SpanEvent(
                    name=str(ev.get("name") or "event"),
                    timestamp=ensure_utc(ev.get("timestamp") or st),
                    attributes=ev_attrs,
                )
            )

    return Span(
        name=redact_text(name),
        context=SpanContext(trace_id=trace_id, span_id=span_id),
        span_kind=sk,
        parent_id=parent_id,
        start_time=st,
        end_time=et,
        status_code=sc,
        status_message=redact_text(status_message or ""),
        attributes=clean_attrs,
        events=span_events,
        conversation=None,
    )


def _fmt_sqlite_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


class SpanStore:
    """Self-contained direct SQLite writer for spans, traces, and project_sessions (no Phoenix server needed)."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        project_name: str = "agent-analytics",
        batch_size: int = 500,
    ) -> None:
        self.db_path = Path(db_path) if db_path else default_spans_db()
        self.project_name = project_name
        self.batch_size = batch_size
        self._buffer: list[Span] = []
        self.total_sent: int = 0
        self._ensure_tables()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.db_path), timeout=30.0)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA busy_timeout=15000;")
        return con

    def _ensure_tables(self) -> None:
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR NOT NULL UNIQUE,
                    description VARCHAR,
                    gradient_start_color VARCHAR DEFAULT '#5bdbff',
                    gradient_end_color VARCHAR DEFAULT '#1c76fc',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    trace_retention_policy_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS project_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id VARCHAR NOT NULL UNIQUE,
                    project_id INTEGER NOT NULL,
                    start_time TIMESTAMP NOT NULL,
                    end_time TIMESTAMP NOT NULL
                );
                CREATE TABLE IF NOT EXISTS traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_rowid INTEGER NOT NULL,
                    trace_id VARCHAR NOT NULL UNIQUE,
                    start_time TIMESTAMP NOT NULL,
                    end_time TIMESTAMP NOT NULL,
                    project_session_rowid INTEGER
                );
                CREATE TABLE IF NOT EXISTS spans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_rowid INTEGER NOT NULL,
                    span_id VARCHAR NOT NULL UNIQUE,
                    parent_id VARCHAR,
                    name VARCHAR NOT NULL,
                    span_kind VARCHAR NOT NULL,
                    start_time TIMESTAMP NOT NULL,
                    end_time TIMESTAMP NOT NULL,
                    attributes JSONB NOT NULL,
                    events JSONB NOT NULL,
                    status_code VARCHAR DEFAULT 'UNSET' NOT NULL,
                    status_message VARCHAR NOT NULL,
                    cumulative_error_count INTEGER NOT NULL DEFAULT 0,
                    cumulative_llm_token_count_prompt INTEGER NOT NULL DEFAULT 0,
                    cumulative_llm_token_count_completion INTEGER NOT NULL DEFAULT 0,
                    llm_token_count_prompt INTEGER,
                    llm_token_count_completion INTEGER
                );
                CREATE INDEX IF NOT EXISTS ix_spans_start_time ON spans (start_time);
                CREATE INDEX IF NOT EXISTS ix_spans_trace_rowid ON spans (trace_rowid);
                CREATE INDEX IF NOT EXISTS ix_spans_parent_id ON spans (parent_id);
                """
            )
            con.execute(
                "INSERT OR IGNORE INTO projects (name, description) VALUES (?, ?)",
                (self.project_name, "Correlated local agent analytics"),
            )
            con.commit()
        finally:
            con.close()

    def add(self, span: Span) -> None:
        self._buffer.append(span)
        if len(self._buffer) >= self.batch_size:
            self.flush()

    def extend(self, spans: list[Span]) -> None:
        self._buffer.extend(spans)
        if len(self._buffer) >= self.batch_size:
            self.flush()

    def flush(self) -> int:
        if not self._buffer:
            return 0
        con = self._connect()
        inserted = 0
        try:
            proj_row = con.execute("SELECT id FROM projects WHERE name = ? LIMIT 1", (self.project_name,)).fetchone()
            project_id = int(proj_row[0]) if proj_row else 2

            while self._buffer:
                chunk = self._buffer[: self.batch_size]
                with con:
                    for sp in chunk:
                        st_str = _fmt_sqlite_ts(sp.start_time)
                        et_str = _fmt_sqlite_ts(sp.end_time)
                        sk_str = sp.span_kind.value if hasattr(sp.span_kind, "value") else str(sp.span_kind)
                        sc_str = sp.status_code.value if hasattr(sp.status_code, "value") else str(sp.status_code)

                        raw_attrs = sp.attributes or {}
                        sess_id = raw_attrs.get("session.id")
                        if not sess_id and isinstance(raw_attrs.get("session"), dict):
                            sess_id = raw_attrs["session"].get("id")

                        sess_rowid: Optional[int] = None
                        if sess_id:
                            con.execute(
                                """
                                INSERT INTO project_sessions (session_id, project_id, start_time, end_time)
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(session_id) DO UPDATE SET
                                    start_time = MIN(project_sessions.start_time, excluded.start_time),
                                    end_time = MAX(project_sessions.end_time, excluded.end_time)
                                """,
                                (str(sess_id), project_id, st_str, et_str),
                            )
                            srow = con.execute("SELECT id FROM project_sessions WHERE session_id = ?", (str(sess_id),)).fetchone()
                            if srow:
                                sess_rowid = int(srow[0])

                        trace_id_str = sp.context.trace_id
                        con.execute(
                            """
                            INSERT INTO traces (project_rowid, trace_id, start_time, end_time, project_session_rowid)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(trace_id) DO UPDATE SET
                                start_time = MIN(traces.start_time, excluded.start_time),
                                end_time = MAX(traces.end_time, excluded.end_time),
                                project_session_rowid = COALESCE(traces.project_session_rowid, excluded.project_session_rowid)
                            """,
                            (project_id, trace_id_str, st_str, et_str, sess_rowid),
                        )
                        trow = con.execute("SELECT id FROM traces WHERE trace_id = ?", (trace_id_str,)).fetchone()
                        trace_rowid = int(trow[0])

                        ptok = raw_attrs.get("llm.token_count.prompt")
                        ctok = raw_attrs.get("llm.token_count.completion")
                        llm_prompt = int(ptok) if ptok is not None else None
                        llm_comp = int(ctok) if ctok is not None else None
                        cum_err = 1 if sc_str == "ERROR" else 0

                        nested_attrs = _unflatten_attributes(raw_attrs, sk_str)
                        events_json = json.dumps(
                            [
                                {
                                    "name": ev.name,
                                    "timestamp": _fmt_sqlite_ts(ev.timestamp),
                                    "attributes": ev.attributes,
                                }
                                for ev in (sp.events or [])
                            ],
                            ensure_ascii=False,
                        )

                        con.execute(
                            """
                            INSERT OR IGNORE INTO spans (
                                trace_rowid, span_id, parent_id, name, span_kind,
                                start_time, end_time, attributes, events,
                                status_code, status_message,
                                cumulative_error_count,
                                cumulative_llm_token_count_prompt,
                                cumulative_llm_token_count_completion,
                                llm_token_count_prompt,
                                llm_token_count_completion
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                trace_rowid,
                                sp.context.span_id,
                                sp.parent_id,
                                sp.name,
                                sk_str,
                                st_str,
                                et_str,
                                json.dumps(nested_attrs, ensure_ascii=False),
                                events_json,
                                sc_str,
                                sp.status_message or "",
                                cum_err,
                                int(llm_prompt or 0),
                                int(llm_comp or 0),
                                llm_prompt,
                                llm_comp,
                            ),
                        )
                del self._buffer[: len(chunk)]
                inserted += len(chunk)
                self.total_sent += len(chunk)
        finally:
            con.close()
        return inserted


PhoenixEmitter = SpanStore  # backward-compatible alias
