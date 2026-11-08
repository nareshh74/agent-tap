"""Declarative TOML adapters: describe a harness's JSONL / JSON-array / SQLite logs, no Python needed.

Example (see examples/adapters/aider.toml)::

    name = "aider"
    kind = "jsonl"                       # jsonl | json_array | sqlite
    [source]
    paths = ["~/.aider/analytics.jsonl"] # globs; ~ and $VARS expanded
    [map]
    timestamp = "time"; name = "event"; session_id = "properties.session"
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import tomllib
from pathlib import Path
from typing import Any, Optional

from ..emitter import SpanStore, build_span, ensure_utc
from ..state import StateStore, make_span_id, make_trace_id
from .base import Context, PollResult

KINDS = ("jsonl", "json_array", "sqlite")
MAP_FIELDS = {
    "timestamp", "end_timestamp", "name", "session_id", "id", "span_kind", "input", "output",
    "model", "tokens_prompt", "tokens_completion", "error",
}
MAX_ROWS = 5000


def _get(obj: Any, path: Optional[str]) -> Any:
    """Dotted-path lookup into dicts/lists ('a.b.0.c')."""
    if not path:
        return None
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur


def validate_data(data: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    if not str(data.get("name") or "").strip():
        errs.append("missing top-level 'name'")
    kind = data.get("kind")
    if kind not in KINDS:
        errs.append(f"'kind' must be one of {KINDS}, got {kind!r}")
    src = data.get("source")
    if not isinstance(src, dict):
        errs.append("missing [source] table")
        src = {}
    elif kind in ("jsonl", "json_array"):
        if not src.get("paths") or not isinstance(src["paths"], list):
            errs.append("[source] needs 'paths' (list of globs)")
    elif kind == "sqlite":
        for k in ("db", "query"):
            if not src.get(k):
                errs.append(f"[source] needs '{k}' for sqlite")
        if src.get("query") and ":last_rowid" not in src["query"]:
            errs.append("sqlite query must reference :last_rowid (and may use :cutoff)")
    m = data.get("map")
    if not isinstance(m, dict):
        errs.append("missing [map] table")
        return errs
    for k in m:
        if k not in MAP_FIELDS and k != "span_kind_default":
            errs.append(f"unknown [map] field '{k}'")
    if not (m.get("timestamp")):
        errs.append("[map] needs 'timestamp'")
    if not (m.get("name") or data.get("name")):
        errs.append("[map] needs 'name'")
    if not m.get("session_id"):
        errs.append("[map] needs 'session_id'")
    return errs


def validate_file(path: str | Path) -> list[str]:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        return [f"cannot parse {path}: {e}"]
    return validate_data(data)


class DeclarativeAdapter:
    def __init__(self, data: dict[str, Any], origin: Optional[Path] = None) -> None:
        self.data = data
        self.origin = origin
        self.name: str = str(data["name"])
        self.kind: str = data["kind"]
        self.src: dict[str, Any] = data["source"]
        self.map: dict[str, Any] = data["map"]
        self.where: dict[str, Any] = data.get("where") or {}

    # -- discovery -----------------------------------------------------
    def _files(self) -> list[str]:
        out: list[str] = []
        for pat in self.src.get("paths", []):
            out.extend(glob.glob(os.path.expandvars(os.path.expanduser(pat)), recursive=True))
        return sorted(set(out))

    def _db(self) -> Optional[Path]:
        return Path(os.path.expandvars(os.path.expanduser(self.src["db"]))) if self.src.get("db") else None

    def discover(self, ctx: Context) -> bool:
        if self.kind == "sqlite":
            db = self._db()
            return bool(db and db.is_file())
        return bool(self._files())

    # -- record -> span --------------------------------------------------
    def _emit(self, rec: dict[str, Any], store: SpanStore, ident: str) -> bool:
        for k, v in self.where.items():
            if _get(rec, k) != v:
                return False
        m = self.map
        ts = _get(rec, m.get("timestamp"))
        if ts is None:
            return False
        sess = _get(rec, m.get("session_id"))
        sess = str(sess) if sess not in (None, "") else "unknown"
        name = _get(rec, m.get("name")) or self.name
        err = _get(rec, m.get("error"))
        sid = _get(rec, m.get("id")) or ident
        end = _get(rec, m.get("end_timestamp"))
        attrs: dict[str, Any] = {
            "harness": self.name,
            "session.id": sess,
            "llm.model_name": _get(rec, m.get("model")),
            "llm.token_count.prompt": _get(rec, m.get("tokens_prompt")),
            "llm.token_count.completion": _get(rec, m.get("tokens_completion")),
        }
        for key, field in (("input.value", "input"), ("output.value", "output")):
            v = _get(rec, m.get(field))
            if v is not None:
                attrs[key] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        for k in ("llm.token_count.prompt", "llm.token_count.completion"):
            try:
                attrs[k] = int(attrs[k]) if attrs[k] is not None else None
            except (TypeError, ValueError):
                attrs[k] = None
        store.add(
            build_span(
                trace_id=make_trace_id(self.name, sess),
                span_id=make_span_id(self.name, sess, sid),
                name=f"{self.name}.{name}",
                span_kind=str(_get(rec, m.get("span_kind")) or m.get("span_kind_default") or "CHAIN"),
                start_time=ensure_utc(ts),
                end_time=ensure_utc(end) if end is not None else None,
                status_code="ERROR" if err else "OK",
                status_message=str(err) if err else "",
                attributes=attrs,
            )
        )
        return True

    # -- polling -------------------------------------------------------------
    def poll(self, ctx: Context, state: StateStore, store: SpanStore, cutoff_ts: float) -> PollResult:
        spans = errs = 0
        before = store.total_sent + len(store._buffer)
        if self.kind == "sqlite":
            self._poll_sqlite(state, store, cutoff_ts)
        else:
            for f in self._files():
                self._poll_file(f, state, store, cutoff_ts)
        spans = store.total_sent + len(store._buffer) - before
        errs = sum(1 for s in store._buffer[-spans:] if spans and s.status_code.value == "ERROR")
        return {self.name: (spans, errs)}

    @staticmethod
    def _tag(rec: dict[str, Any], path: str) -> dict[str, Any]:
        """Expose _file (stem) and _dir (parent dir name) so a map field can use them as session id."""
        p = Path(path)
        return {**rec, "_file": p.stem, "_dir": p.parent.name}

    def _poll_file(self, path: str, state: StateStore, store: SpanStore, cutoff_ts: float) -> None:
        try:
            st = os.stat(path)
        except OSError:
            return
        if st.st_mtime < cutoff_ts:
            return
        offset, mtime_ns, _ = state.get_file_watermark(path)
        if self.kind == "json_array":
            if st.st_mtime_ns == mtime_ns:
                return
            try:
                arr = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                return
            if not isinstance(arr, list):
                return
            for i, rec in enumerate(arr[offset:], start=offset):
                if isinstance(rec, dict):
                    self._emit(self._tag(rec, path), store, f"{Path(path).name}:{i}")
            state.set_file_watermark(self.name, path, len(arr), st.st_mtime_ns)
            return
        if st.st_size < offset:  # truncated / rotated
            offset = 0
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
        end = data.rfind(b"\n") + 1  # only whole lines
        for n, line in enumerate(data[:end].splitlines()):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                self._emit(self._tag(rec, path), store, f"{Path(path).name}:{offset}:{n}")
        state.set_file_watermark(self.name, path, offset + end, st.st_mtime_ns)

    def _poll_sqlite(self, state: StateStore, store: SpanStore, cutoff_ts: float) -> None:
        db = self._db()
        if not db or not db.is_file():
            return
        table = str(self.src.get("table") or "query")
        last, _ = state.get_db_watermark(self.name, db, table)
        rowid_col = str(self.src.get("rowid_column") or "rowid")
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                f"SELECT * FROM ({self.src['query'].rstrip().rstrip(';')}) LIMIT {MAX_ROWS}",
                {"last_rowid": last or 0, "cutoff": cutoff_ts},
            ).fetchall()
        finally:
            con.close()
        top = last or 0
        for r in rows:
            rec = dict(r)
            rid = rec.get(rowid_col)
            if self._emit(rec, store, f"{db.name}:{rid}") and isinstance(rid, int):
                top = max(top, rid)
            elif isinstance(rid, int):
                top = max(top, rid)
        if rows:
            state.set_db_watermark(self.name, db, table, last_rowid=top)


def load_declarative_adapters(directory: str | Path) -> list[DeclarativeAdapter]:
    d = Path(directory)
    out: list[DeclarativeAdapter] = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.toml")):
        try:
            data = tomllib.loads(f.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if not validate_data(data):
            out.append(DeclarativeAdapter(data, f))
    return out
