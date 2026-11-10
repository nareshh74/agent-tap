import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def make_trace_id(harness: str, session_id: str) -> str:
    return hashlib.sha256(f"{harness}:session:{session_id}".encode("utf-8")).hexdigest()[:32]


def make_span_id(*parts: object) -> str:
    joined = ":".join(str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


@dataclass
class SessionLink:
    child_harness: str
    child_session_id: str
    raven_session_key: str
    raven_call_id: str
    raven_node_span_id: str
    trace_id: str
    started_at_ms: Optional[int]
    ended_at_ms: Optional[int]
    confidence: str


class StateStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        from .core.paths import default_state_db

        self.db_path = Path(db_path) if db_path else default_state_db()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=60.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS file_watermarks (
                source TEXT NOT NULL,
                file_path TEXT PRIMARY KEY,
                inode_or_ctime INTEGER,
                byte_offset INTEGER NOT NULL DEFAULT 0,
                mtime_ns INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS db_watermarks (
                source TEXT NOT NULL,
                db_path TEXT NOT NULL,
                table_name TEXT NOT NULL,
                last_rowid INTEGER,
                last_ts TEXT,
                updated_at REAL NOT NULL,
                PRIMARY KEY (source, db_path, table_name)
            );

            CREATE TABLE IF NOT EXISTS session_links (
                child_harness TEXT NOT NULL,
                child_session_id TEXT NOT NULL,
                raven_session_key TEXT NOT NULL,
                raven_call_id TEXT NOT NULL,
                raven_node_span_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                started_at_ms INTEGER,
                ended_at_ms INTEGER,
                confidence TEXT NOT NULL,
                PRIMARY KEY (child_harness, child_session_id, raven_call_id)
            );

            CREATE INDEX IF NOT EXISTS ix_session_links_child
                ON session_links (child_harness, child_session_id);

            CREATE TABLE IF NOT EXISTS error_buckets (
                bucket_key TEXT PRIMARY KEY,
                harness TEXT NOT NULL,
                profile TEXT,
                error_class TEXT NOT NULL,
                window_start INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                first_ts REAL NOT NULL,
                last_ts REAL NOT NULL,
                sample_message TEXT,
                emitted_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS source_health (
                source TEXT PRIMARY KEY,
                last_poll_at REAL NOT NULL,
                last_event_at REAL,
                spans_emitted INTEGER NOT NULL DEFAULT 0,
                errors_emitted INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            );
            """
        )
        self.conn.commit()

    def get_file_watermark(self, file_path: str | Path) -> tuple[int, int, Optional[int]]:
        p = os.path.normcase(os.path.abspath(str(file_path)))
        row = self.conn.execute(
            "SELECT byte_offset, mtime_ns, inode_or_ctime FROM file_watermarks WHERE file_path = ?",
            (p,),
        ).fetchone()
        if not row:
            return 0, 0, None
        return int(row["byte_offset"]), int(row["mtime_ns"]), row["inode_or_ctime"]

    def set_file_watermark(
        self,
        source: str,
        file_path: str | Path,
        byte_offset: int,
        mtime_ns: int,
        inode_or_ctime: Optional[int] = None,
    ) -> None:
        p = os.path.normcase(os.path.abspath(str(file_path)))
        try:
            self.conn.execute(
                """
                INSERT INTO file_watermarks (source, file_path, inode_or_ctime, byte_offset, mtime_ns, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    source = excluded.source,
                    inode_or_ctime = excluded.inode_or_ctime,
                    byte_offset = excluded.byte_offset,
                    mtime_ns = excluded.mtime_ns,
                    updated_at = excluded.updated_at
                """,
                (source, p, inode_or_ctime, int(byte_offset), int(mtime_ns), time.time()),
            )
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

    def get_db_watermark(self, source: str, db_path: str | Path, table_name: str) -> tuple[Optional[int], Optional[str]]:
        p = os.path.normcase(os.path.abspath(str(db_path)))
        row = self.conn.execute(
            "SELECT last_rowid, last_ts FROM db_watermarks WHERE source = ? AND db_path = ? AND table_name = ?",
            (source, p, table_name),
        ).fetchone()
        if not row:
            return None, None
        return row["last_rowid"], row["last_ts"]

    def set_db_watermark(
        self,
        source: str,
        db_path: str | Path,
        table_name: str,
        last_rowid: Optional[int] = None,
        last_ts: Optional[str] = None,
    ) -> None:
        p = os.path.normcase(os.path.abspath(str(db_path)))
        try:
            self.conn.execute(
                """
                INSERT INTO db_watermarks (source, db_path, table_name, last_rowid, last_ts, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, db_path, table_name) DO UPDATE SET
                    last_rowid = COALESCE(excluded.last_rowid, db_watermarks.last_rowid),
                    last_ts = COALESCE(excluded.last_ts, db_watermarks.last_ts),
                    updated_at = excluded.updated_at
                """,
                (source, p, table_name, last_rowid, last_ts, time.time()),
            )
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

    def upsert_session_link(self, link: SessionLink) -> None:
        try:
            self.conn.execute(
                """
                INSERT INTO session_links (
                    child_harness, child_session_id, raven_session_key, raven_call_id,
                    raven_node_span_id, trace_id, started_at_ms, ended_at_ms, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(child_harness, child_session_id, raven_call_id) DO UPDATE SET
                    raven_session_key = excluded.raven_session_key,
                    raven_node_span_id = excluded.raven_node_span_id,
                    trace_id = excluded.trace_id,
                    started_at_ms = COALESCE(excluded.started_at_ms, session_links.started_at_ms),
                    ended_at_ms = COALESCE(excluded.ended_at_ms, session_links.ended_at_ms),
                    confidence = excluded.confidence
                """,
                (
                    link.child_harness,
                    link.child_session_id,
                    link.raven_session_key,
                    link.raven_call_id,
                    link.raven_node_span_id,
                    link.trace_id,
                    link.started_at_ms,
                    link.ended_at_ms,
                    link.confidence,
                ),
            )
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

    def resolve_session_link(
        self,
        child_harness: str,
        child_session_id: str,
        event_ts_ms: Optional[int] = None,
    ) -> Optional[SessionLink]:
        rows = self.conn.execute(
            """
            SELECT * FROM session_links
            WHERE child_harness = ? AND child_session_id = ?
            ORDER BY COALESCE(started_at_ms, 0) ASC
            """,
            (child_harness, child_session_id),
        ).fetchall()
        if not rows:
            return None
        if len(rows) == 1 or event_ts_ms is None:
            r = rows[-1]
        else:
            matched = None
            for r in rows:
                s = r["started_at_ms"] or 0
                e = r["ended_at_ms"] or (s + 3_600_000)
                if (s - 15_000) <= event_ts_ms <= (e + 15_000):
                    matched = r
                    break
            if matched is None:
                # Pick closest by started_at_ms
                matched = min(rows, key=lambda r: abs((r["started_at_ms"] or 0) - event_ts_ms))
            r = matched
        return SessionLink(
            child_harness=r["child_harness"],
            child_session_id=r["child_session_id"],
            raven_session_key=r["raven_session_key"],
            raven_call_id=r["raven_call_id"],
            raven_node_span_id=r["raven_node_span_id"],
            trace_id=r["trace_id"],
            started_at_ms=r["started_at_ms"],
            ended_at_ms=r["ended_at_ms"],
            confidence=r["confidence"],
        )

    def record_source_health(
        self,
        source: str,
        spans_emitted: int = 0,
        errors_emitted: int = 0,
        last_event_at: Optional[float] = None,
        last_error: Optional[str] = None,
    ) -> None:
        now = time.time()
        try:
            self.conn.execute(
                """
                INSERT INTO source_health (source, last_poll_at, last_event_at, spans_emitted, errors_emitted, last_error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    last_poll_at = excluded.last_poll_at,
                    last_event_at = COALESCE(excluded.last_event_at, source_health.last_event_at),
                    spans_emitted = source_health.spans_emitted + excluded.spans_emitted,
                    errors_emitted = source_health.errors_emitted + excluded.errors_emitted,
                    last_error = excluded.last_error
                """,
                (source, now, last_event_at, spans_emitted, errors_emitted, last_error),
            )
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

    def commit(self) -> None:
        try:
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

    def close(self) -> None:
        self.conn.close()
