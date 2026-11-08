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

_ROOT: Path = Path()
_UUID_RE = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", re.IGNORECASE)
_COPILOT_LOG_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+\[(?P<level>[A-Z]+)\]\s+(?P<msg>.*)$")
def poll_codex(
    store: StateStore,
    emitter: PhoenixEmitter,
    cutoff_ts: float,
) -> tuple[int, int]:
    spans_count = 0
    errors_count = 0
    if not _ROOT.exists():
        return 0, 0

    for xf in sorted(glob.glob(str(_ROOT / "sessions" / "**" / "*.jsonl"), recursive=True)):
        try:
            st = os.stat(xf)
        except OSError:
            continue
        if st.st_mtime < cutoff_ts:
            continue

        offset, prev_mtime_ns, prev_ctime = store.get_file_watermark(xf)
        if st.st_size < offset or (prev_ctime and int(st.st_ctime) != prev_ctime):
            offset = 0
        if st.st_size == offset and st.st_mtime_ns == prev_mtime_ns:
            continue

        thread_id = Path(xf).stem.split("-")[-5:]
        thread_id_str = "-".join(thread_id) if len(thread_id) == 5 else Path(xf).stem
        root_session_id = thread_id_str
        parent_thread_id: Optional[str] = None
        cwd = ""
        model_provider = ""
        started_ts = st.st_ctime

        try:
            with open(xf, "r", encoding="utf-8", errors="replace") as f:
                first_line = f.readline()
                if first_line:
                    d0 = json.loads(first_line)
                    if d0.get("type") == "session_meta":
                        pl = d0.get("payload") or {}
                        thread_id_str = pl.get("id") or thread_id_str
                        root_session_id = pl.get("session_id") or thread_id_str
                        parent_thread_id = pl.get("parent_thread_id")
                        cwd = pl.get("cwd") or ""
                        model_provider = pl.get("model_provider") or ""
                        started_ts = pl.get("timestamp") or d0.get("timestamp") or started_ts
        except Exception:
            pass

        is_subagent = bool(parent_thread_id and parent_thread_id != thread_id_str)
        lookup_sid = parent_thread_id if is_subagent else root_session_id
        st_dt = ensure_utc(started_ts)
        link = store.resolve_session_link("codex", lookup_sid, int(st_dt.timestamp() * 1000))

        if link:
            trace_id = link.trace_id
            top_sess_id = link.raven_session_key
            if is_subagent and parent_thread_id:
                parent_span_id = make_span_id("codex", "session", parent_thread_id)
                conf = "exact_2nd_degree"
            else:
                parent_span_id = link.raven_node_span_id
                conf = link.confidence
        else:
            trace_id = make_trace_id("codex", root_session_id)
            top_sess_id = root_session_id
            parent_span_id = make_span_id("codex", "session", parent_thread_id) if (is_subagent and parent_thread_id) else None
            conf = "exact_codex_subagent" if is_subagent else "standalone"

        sess_span_id = make_span_id("codex", "session", thread_id_str)
        if offset == 0:
            emitter.add(
                build_span(
                    trace_id=trace_id,
                    span_id=sess_span_id,
                    name=f"codex.{'subagent' if is_subagent else 'session'} ({thread_id_str[:18]})",
                    span_kind="AGENT",
                    parent_id=parent_span_id,
                    start_time=started_ts,
                    end_time=st.st_mtime,
                    status_code="OK",
                    attributes={
                        "session.id": top_sess_id,
                        "child.session_id": thread_id_str,
                        "codex.root_session_id": root_session_id,
                        "codex.parent_thread_id": parent_thread_id or "",
                        "harness": "codex",
                        "agent.name": "Codex Subagent" if is_subagent else "Codex",
                        "llm.provider": model_provider,
                        "working_directory": cwd,
                        "correlation.confidence": conf,
                    },
                )
            )
            spans_count += 1

        try:
            with open(xf, "rb") as bf:
                bf.seek(offset)
                raw = bf.read()
                new_offset = bf.tell()
        except OSError:
            continue

        for idx, line in enumerate(raw.decode("utf-8", errors="replace").splitlines()):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = rec.get("timestamp") or st.st_mtime
            ordinal = rec.get("ordinal", offset + idx)
            rtype = rec.get("type")
            pl = rec.get("payload") or {}
            if not isinstance(pl, dict):
                continue

            if rtype == "event_msg":
                ev_type = pl.get("type")
                if ev_type == "task_started":
                    turn_id = pl.get("turn_id") or str(ordinal)
                    emitter.add(
                        build_span(
                            trace_id=trace_id,
                            span_id=make_span_id("codex", "turn", thread_id_str, turn_id),
                            name=f"codex.turn ({turn_id[:8]})",
                            span_kind="CHAIN",
                            parent_id=sess_span_id,
                            start_time=ts,
                            end_time=ts,
                            status_code="OK",
                            attributes={
                                "session.id": top_sess_id,
                                "child.session_id": thread_id_str,
                                "harness": "codex",
                                "codex.turn_id": turn_id,
                            },
                        )
                    )
                    spans_count += 1
                elif ev_type == "task_complete":
                    turn_id = pl.get("turn_id") or str(ordinal)
                    err_obj = pl.get("error")
                    if err_obj:
                        err_msg = str(err_obj.get("message") if isinstance(err_obj, dict) else err_obj)
                        err_cls = "omniroute_401_unauthorized" if "401 Unauthorized" in err_msg else "codex_turn_error"
                        errors_count += 1
                        emitter.add(
                            build_span(
                                trace_id=trace_id,
                                span_id=make_span_id("codex", "turn_err", thread_id_str, turn_id, ordinal),
                                name=f"codex.error.{err_cls}",
                                span_kind="LLM",
                                parent_id=sess_span_id,
                                start_time=ts,
                                end_time=ts,
                                status_code="ERROR",
                                status_message=err_msg[:1000],
                                attributes={
                                    "session.id": top_sess_id,
                                    "child.session_id": thread_id_str,
                                    "harness": "codex",
                                    "agent.name": "Codex",
                                    "codex.turn_id": turn_id,
                                    "error.class": err_cls,
                                    "output.value": err_msg,
                                },
                            )
                        )
                        spans_count += 1
                elif ev_type == "token_count":
                    info = pl.get("info") or {}
                    last_u = (info.get("last_token_usage") or {}) if isinstance(info, dict) else {}
                    in_tok = int(last_u.get("input_tokens") or 0) + int(last_u.get("cached_input_tokens") or 0)
                    out_tok = int(last_u.get("output_tokens") or 0)
                    if in_tok or out_tok:
                        emitter.add(
                            build_span(
                                trace_id=trace_id,
                                span_id=make_span_id("codex", "usage", thread_id_str, ordinal),
                                name="codex.llm.usage",
                                span_kind="LLM",
                                parent_id=sess_span_id,
                                start_time=ts,
                                end_time=ts,
                                status_code="OK",
                                attributes={
                                    "session.id": top_sess_id,
                                    "child.session_id": thread_id_str,
                                    "harness": "codex",
                                    "llm.provider": model_provider or "omniroute",
                                    "llm.token_count.prompt": in_tok,
                                    "llm.token_count.completion": out_tok,
                                    "llm.token_count.total": in_tok + out_tok,
                                },
                            )
                        )
                        spans_count += 1

            elif rtype == "response_item":
                p_type = pl.get("type")
                if p_type in ("custom_tool_call", "function_call"):
                    tname = pl.get("name") or "tool"
                    cid = pl.get("call_id") or pl.get("id") or str(ordinal)
                    targs = pl.get("arguments") or pl.get("input") or ""
                    emitter.add(
                        build_span(
                            trace_id=trace_id,
                            span_id=make_span_id("codex", "tool", thread_id_str, cid),
                            name=f"codex.tool.{tname}",
                            span_kind="TOOL",
                            parent_id=sess_span_id,
                            start_time=ts,
                            end_time=ts,
                            status_code="OK",
                            attributes={
                                "session.id": top_sess_id,
                                "child.session_id": thread_id_str,
                                "harness": "codex",
                                "tool.name": tname,
                                "input.value": str(targs)[:8000],
                            },
                        )
                    )
                    spans_count += 1
                elif p_type == "message" and pl.get("role") in ("user", "assistant"):
                    role = pl.get("role")
                    c_items = pl.get("content") or []
                    txt = "\n".join(str(x.get("text") or "") for x in c_items if isinstance(x, dict))
                    emitter.add(
                        build_span(
                            trace_id=trace_id,
                            span_id=make_span_id("codex", "msg", thread_id_str, ordinal),
                            name=f"codex.message.{role}",
                            span_kind="LLM" if role == "assistant" else "CHAIN",
                            parent_id=sess_span_id,
                            start_time=ts,
                            end_time=ts,
                            status_code="OK",
                            attributes={
                                "session.id": top_sess_id,
                                "child.session_id": thread_id_str,
                                "harness": "codex",
                                "input.value" if role == "user" else "output.value": txt[:12000],
                            },
                        )
                    )
                    spans_count += 1

        store.set_file_watermark("codex_rollout", xf, new_offset, st.st_mtime_ns, int(st.st_ctime))

    # Poll Codex logs_2.sqlite for WARN/ERROR/FATAL rows
    logs_db = _ROOT / "logs_2.sqlite"
    if logs_db.exists():
        last_rowid, _ = store.get_db_watermark("codex_logs_db", logs_db, "logs")
        last_rowid = int(last_rowid or 0)
        try:
            con = sqlite3.connect(f"file:{logs_db.as_posix()}?mode=ro", uri=True, timeout=5.0)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT id, ts, level, target, feedback_log_body, thread_id
                FROM logs
                WHERE id > ? AND ts >= ? AND level IN ('WARN', 'ERROR', 'FATAL')
                ORDER BY id ASC
                """,
                (last_rowid, int(cutoff_ts)),
            ).fetchall()
            max_id = last_rowid
            for r in rows:
                rid = int(r["id"])
                if rid > max_id:
                    max_id = rid
                th_id = r["thread_id"] or "codex:system"
                ts_sec = float(r["ts"] or cutoff_ts)
                body = str(r["feedback_log_body"] or "")
                target = str(r["target"] or "")
                lvl = str(r["level"] or "WARN")

                link = store.resolve_session_link("codex", th_id, int(ts_sec * 1000))
                trace_id = link.trace_id if link else make_trace_id("codex", th_id)
                top_sess_id = link.raven_session_key if link else th_id
                parent_id = make_span_id("codex", "session", th_id) if r["thread_id"] else None

                err_cls = "codex_responses_retry_warn" if "responses_retry" in target else f"codex_{lvl.lower()}_{target.replace('::', '_')}"
                emitter.add(
                    build_span(
                        trace_id=trace_id,
                        span_id=make_span_id("codex_sqlite_log", rid),
                        name=f"codex.log.{err_cls}",
                        span_kind="CHAIN",
                        parent_id=parent_id,
                        start_time=ts_sec,
                        end_time=ts_sec,
                        status_code="ERROR" if lvl in ("ERROR", "FATAL") or "responses_retry" in target else "OK",
                        status_message=body[:500],
                        attributes={
                            "session.id": top_sess_id,
                            "child.session_id": th_id,
                            "harness": "codex",
                            "log.level": lvl,
                            "log.target": target,
                            "error.class": err_cls if (lvl in ("ERROR", "FATAL") or "responses_retry" in target) else None,
                            "output.value": body[:4000],
                        },
                    )
                )
                spans_count += 1
                if lvl in ("ERROR", "FATAL") or "responses_retry" in target:
                    errors_count += 1

            if max_id > last_rowid:
                store.set_db_watermark("codex_logs_db", logs_db, "logs", last_rowid=max_id)
            con.close()
        except Exception:
            pass

    return spans_count, errors_count


# ============================================================================


class CodexAdapter:
    name = "codex"

    def discover(self, ctx: Context) -> bool:
        return self._root(ctx).is_dir()

    @staticmethod
    def _root(ctx: Context) -> Path:
        return ctx.dir_for("codex", paths.codex_root())

    def poll(self, ctx: Context, state: StateStore, store: PhoenixEmitter, cutoff_ts: float) -> PollResult:
        global _ROOT
        _ROOT = self._root(ctx)
        out: PollResult = {}
        try:
            out["codex"] = poll_codex(state, store, cutoff_ts)
        except Exception as e:
            state.record_source_health("codex", last_error=str(e))
            out["codex"] = (0, 0)
        return out
