import glob
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..core import paths
from ..emitter import SpanStore as PhoenixEmitter, build_span, ensure_utc
from ..state import SessionLink, StateStore, make_span_id, make_trace_id
from .base import Context, PollResult

_DIR: Optional[Path] = None
def _query_omniroute_sqlite(cutoff_iso: str, last_call_ts: str, last_usage_id: int) -> dict[str, list[dict[str, Any]]]:
    """Query OmniRoute's live WAL SQLite database via docker exec node (better-sqlite3 readonly) with UNC immutable fallback."""
    js = f"""
    const Database = require('better-sqlite3');
    const db = new Database('/app/data/storage.sqlite', {{readonly: true, timeout: 5000}});
    const calls = db.prepare(`
        SELECT id, timestamp, method, path, status, model, requested_model, provider,
               duration, ttft_ms, tokens_in, tokens_out, tokens_cache_read,
               combo_name, error_summary, error_type, correlation_id, session_tag, response_id
        FROM call_logs
        WHERE timestamp > ?
        ORDER BY timestamp ASC
        LIMIT 2000
    `).all({json.dumps(max(cutoff_iso, last_call_ts))});
    const usage = db.prepare(`
        SELECT id, timestamp, provider, model, status, success, latency_ms, ttft_ms,
               tokens_input, tokens_output, error_code, endpoint, combo_strategy
        FROM usage_history
        WHERE id > ? AND timestamp >= ? AND (status != '200' OR success != 1)
        ORDER BY id ASC
        LIMIT 2000
    `).all({int(last_usage_id)}, {json.dumps(cutoff_iso)});
    db.close();
    console.log(JSON.stringify({{calls, usage}}));
    """
    try:
        if _DIR is not None:
            raise OSError("direct file access preferred")
        r = subprocess.run(
            ["docker", "exec", "omniroute", "node", "-e", js],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        pass

    if _DIR is None:
        return {"calls": [], "usage": []}
    db_file = _DIR / "storage.sqlite"
    if not db_file.exists():
        return {"calls": [], "usage": []}
    con = sqlite3.connect(f"file:{db_file}?mode=ro&immutable=1", uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    calls = [
        dict(r)
        for r in con.execute(
            """
            SELECT id, timestamp, method, path, status, model, requested_model, provider,
                   duration, ttft_ms, tokens_in, tokens_out, tokens_cache_read,
                   combo_name, error_summary, error_type, correlation_id, session_tag, response_id
            FROM call_logs
            WHERE timestamp > ?
            ORDER BY timestamp ASC
            LIMIT 2000
            """,
            (max(cutoff_iso, last_call_ts),),
        ).fetchall()
    ]
    usage = [
        dict(r)
        for r in con.execute(
            """
            SELECT id, timestamp, provider, model, status, success, latency_ms, ttft_ms,
                   tokens_input, tokens_output, error_code, endpoint, combo_strategy
            FROM usage_history
            WHERE id > ? AND timestamp >= ? AND (status != '200' OR success != 1)
            ORDER BY id ASC
            LIMIT 2000
            """,
            (int(last_usage_id), cutoff_iso),
        ).fetchall()
    ]
    con.close()
    return {"calls": calls, "usage": usage}


def poll_omniroute(
    store: StateStore,
    emitter: PhoenixEmitter,
    cutoff_ts: float,
) -> tuple[int, int]:
    spans_count = 0
    errors_count = 0

    cutoff_iso = ensure_utc(cutoff_ts).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    db_key = "omniroute:/app/data/storage.sqlite"
    _, last_call_ts = store.get_db_watermark("omniroute_calls", db_key, "call_logs")
    last_call_ts = last_call_ts or cutoff_iso

    last_usage_id, _ = store.get_db_watermark("omniroute_usage", db_key, "usage_history")
    last_usage_id = int(last_usage_id or 0)

    while True:
        batch = _query_omniroute_sqlite(cutoff_iso, last_call_ts, last_usage_id)
        calls = batch.get("calls") or []
        usage = batch.get("usage") or []
        if not calls and not usage:
            break

        for c in calls:
            cid = str(c.get("id") or "")
            ts_str = str(c.get("timestamp") or cutoff_iso)
            if ts_str > last_call_ts:
                last_call_ts = ts_str
            end_dt = ensure_utc(ts_str)
            dur_ms = int(c.get("duration") or 0)
            from datetime import timedelta
            start_dt = end_dt - timedelta(milliseconds=max(dur_ms, 1))

            status = int(c.get("status") or 200)
            is_err = status >= 400
            if is_err:
                errors_count += 1
            sess_tag = c.get("session_tag") or "omniroute:router"
            trace_id = make_trace_id("omniroute", sess_tag)
            in_tok = int(c.get("tokens_in") or 0) + int(c.get("tokens_cache_read") or 0)
            out_tok = int(c.get("tokens_out") or 0)
            err_sum = str(c.get("error_summary") or "")
            err_cls = f"omniroute_http_{status}" if is_err else None

            emitter.add(
                build_span(
                    trace_id=trace_id,
                    span_id=make_span_id("omniroute_call", cid, ts_str),
                    name=f"omniroute.call {c.get('path')} [{c.get('requested_model') or c.get('model')}] ({status})",
                    span_kind="LLM",
                    parent_id=None,
                    start_time=start_dt,
                    end_time=end_dt,
                    status_code="ERROR" if is_err else "OK",
                    status_message=err_sum[:500] if is_err else "",
                    attributes={
                        "session.id": sess_tag,
                        "harness": "omniroute",
                        "agent.name": "OmniRoute",
                        "http.status_code": status,
                        "http.path": c.get("path") or "",
                        "llm.model_name": c.get("requested_model") or c.get("model") or "",
                        "llm.provider": c.get("provider") or "",
                        "llm.token_count.prompt": in_tok,
                        "llm.token_count.completion": out_tok,
                        "llm.token_count.total": in_tok + out_tok,
                        "omniroute.duration_ms": dur_ms,
                        "omniroute.ttft_ms": c.get("ttft_ms"),
                        "omniroute.combo_name": c.get("combo_name") or "",
                        "omniroute.correlation_id": c.get("correlation_id") or "",
                        "omniroute.response_id": c.get("response_id") or "",
                        "error.class": err_cls,
                        "output.value": err_sum,
                    },
                )
            )
            spans_count += 1

        if calls:
            store.set_db_watermark("omniroute_calls", db_key, "call_logs", last_ts=last_call_ts)

        for u in usage:
            uid = int(u.get("id") or 0)
            if uid > last_usage_id:
                last_usage_id = uid
            ts_str = str(u.get("timestamp") or cutoff_iso)
            end_dt = ensure_utc(ts_str)
            lat_ms = int(u.get("latency_ms") or 0)
            from datetime import timedelta
            start_dt = end_dt - timedelta(milliseconds=max(lat_ms, 1))
            status_str = str(u.get("status") or "500")
            err_code = str(u.get("error_code") or f"http_{status_str}")
            err_cls = f"omniroute_{err_code}"

            emitter.add(
                build_span(
                    trace_id=make_trace_id("omniroute", "upstream_errors"),
                    span_id=make_span_id("omniroute_usage_err", uid),
                    name=f"omniroute.upstream_error [{u.get('provider')}/{u.get('model')}] ({status_str}: {err_code})",
                    span_kind="LLM",
                    parent_id=None,
                    start_time=start_dt,
                    end_time=end_dt,
                    status_code="ERROR",
                    status_message=f"{u.get('provider')}/{u.get('model')} status={status_str} error_code={err_code}",
                    attributes={
                        "session.id": "omniroute:upstream",
                        "harness": "omniroute",
                        "agent.name": "OmniRoute",
                        "llm.provider": u.get("provider") or "",
                        "llm.model_name": u.get("model") or "",
                        "http.status_code": status_str,
                        "omniroute.latency_ms": lat_ms,
                        "error.class": err_cls,
                        "output.value": err_code,
                    },
                )
            )
            spans_count += 1
            errors_count += 1

        if usage:
            store.set_db_watermark("omniroute_usage", db_key, "usage_history", last_rowid=last_usage_id)

        if len(calls) < 2000 and len(usage) < 2000:
            break

    # Also tail OmniRoute Pino application logs (for pre-dispatch 401 'No active credentials for provider: codex')
    app_log_dir = (_DIR / "logs" / "application") if _DIR else None
    if app_log_dir is not None and app_log_dir.exists():
        for alog in sorted(glob.glob(str(app_log_dir / "app*.log"))):
            try:
                st = os.stat(alog)
            except OSError:
                continue
            if st.st_mtime < cutoff_ts:
                continue
            offset, prev_mtime_ns, prev_ctime = store.get_file_watermark(alog)
            if st.st_size < offset or (prev_ctime and int(st.st_ctime) != prev_ctime):
                offset = 0
            if st.st_size == offset and st.st_mtime_ns == prev_mtime_ns:
                continue

            try:
                with open(alog, "rb") as bf:
                    bf.seek(offset)
                    raw = bf.read()
                    new_offset = bf.tell()
            except OSError:
                continue

            for idx, line in enumerate(raw.decode("utf-8", errors="replace").splitlines()):
                if '"level":40' not in line and '"level":50' not in line and '"warn"' not in line and '"error"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts_str = rec.get("time") or rec.get("timestamp")
                if not ts_str:
                    continue
                dt = ensure_utc(ts_str)
                if dt.timestamp() < cutoff_ts:
                    continue
                msg = str(rec.get("msg") or rec.get("message") or "")
                tag = str(rec.get("tag") or rec.get("component") or "")

                err_cls: Optional[str] = None
                if "No active credentials for provider: codex" in msg:
                    err_cls = "omniroute_codex_401_no_active_credentials"
                elif "CLOSED → DEGRADED" in msg or "OPEN" in msg:
                    err_cls = "omniroute_circuit_degraded"
                elif "Compression pipeline error" in msg:
                    err_cls = "omniroute_compression_regex_error"
                elif rec.get("level") == 50 or rec.get("level") == "error":
                    err_cls = "omniroute_app_error"

                if not err_cls:
                    continue

                emitter.add(
                    build_span(
                        trace_id=make_trace_id("omniroute", "app_log"),
                        span_id=make_span_id("omniroute_applog", Path(alog).name, offset + idx),
                        name=f"omniroute.error.{err_cls}",
                        span_kind="CHAIN",
                        parent_id=None,
                        start_time=dt,
                        end_time=dt,
                        status_code="ERROR",
                        status_message=msg[:500],
                        attributes={
                            "session.id": "omniroute:app",
                            "harness": "omniroute",
                            "agent.name": "OmniRoute",
                            "omniroute.tag": tag,
                            "error.class": err_cls,
                            "output.value": msg[:4000],
                        },
                    )
                )
                spans_count += 1
                errors_count += 1

            store.set_file_watermark("omniroute_app_log", alog, new_offset, st.st_mtime_ns, int(st.st_ctime))

    return spans_count, errors_count


class OmniRouteAdapter:
    name = "omniroute"

    def discover(self, ctx: Context) -> bool:
        return self._dir(ctx) is not None or bool(self._docker())

    @staticmethod
    def _dir(ctx: Context) -> Optional[Path]:
        o = ctx.dir_for("omniroute")
        if o:
            return o
        found = paths.omniroute_dirs(ctx.config.wsl_discovery)
        return found[0] if found else None

    @staticmethod
    def _docker() -> bool:
        import shutil

        return shutil.which("docker") is not None

    def poll(self, ctx: Context, state: StateStore, store: PhoenixEmitter, cutoff_ts: float) -> PollResult:
        global _DIR
        _DIR = self._dir(ctx)
        out: PollResult = {}
        try:
            out["omniroute"] = poll_omniroute(state, store, cutoff_ts)
        except Exception as e:
            state.record_source_health("omniroute", last_error=str(e))
            out["omniroute"] = (0, 0)
        return out
