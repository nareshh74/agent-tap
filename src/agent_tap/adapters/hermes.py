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

_PROFILES: list[tuple[str, Path]] = []
_HERMES_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d{3})\s+(?P<level>WARNING|ERROR|CRITICAL|INFO)\s+(?P<logger>[^:]+):\s*(?P<msg>.*)$"
)
_MCP_SETUP_RE = re.compile(r"MCP OAuth setup failed for '([^']+)'")


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def poll_hermes_state_dbs(
    store: StateStore,
    emitter: PhoenixEmitter,
    cutoff_ts: float,
) -> tuple[int, int]:
    spans_count = 0
    errors_count = 0

    for profile_name, prof_dir in _PROFILES:
        db_file = prof_dir / "state.db"
        if not db_file.exists():
            continue

        _, last_sess_ts_str = store.get_db_watermark("hermes_state_db", db_file, "sessions")
        last_sess_ts = float(last_sess_ts_str) if last_sess_ts_str is not None else cutoff_ts
        effective_sess_cutoff = max(last_sess_ts, cutoff_ts)

        last_msg_id, _ = store.get_db_watermark("hermes_state_db", db_file, "messages")
        last_msg_id = int(last_msg_id or 0)

        try:
            con = _connect_readonly(db_file)
        except Exception:
            continue

        try:
            sess_rows = con.execute(
                """
                SELECT id, source, created_source, parent_session_id, model, model_config,
                       started_at, ended_at, last_activity_at, end_reason,
                       message_count, tool_call_count, input_tokens, output_tokens,
                       cache_read_tokens, cache_write_tokens, reasoning_tokens,
                       cwd, billing_provider, billing_base_url, title
                FROM sessions
                WHERE COALESCE(last_activity_at, started_at, 0) >= ?
                ORDER BY COALESCE(last_activity_at, started_at, 0) ASC
                """,
                (effective_sess_cutoff,),
            ).fetchall()

            session_map: dict[str, sqlite3.Row] = {}
            max_sess_ts = last_sess_ts
            for sr in sess_rows:
                sid = sr["id"]
                session_map[sid] = sr
                act_ts = float(sr["last_activity_at"] or sr["started_at"] or 0.0)
                if act_ts > max_sess_ts:
                    max_sess_ts = act_ts

                st_sec = float(sr["started_at"] or act_ts or cutoff_ts)
                en_sec = float(sr["ended_at"] or act_ts or st_sec)
                link = store.resolve_session_link("hermes", sid, int(st_sec * 1000))
                if not link and sr["parent_session_id"]:
                    link = store.resolve_session_link("hermes", str(sr["parent_session_id"]), int(st_sec * 1000))

                if link:
                    trace_id = link.trace_id
                    parent_span_id = link.raven_node_span_id
                    top_sess_id = link.raven_session_key
                    conf = link.confidence
                elif sr["parent_session_id"]:
                    trace_id = make_trace_id("hermes", str(sr["parent_session_id"]))
                    parent_span_id = make_span_id("hermes", "session", profile_name, str(sr["parent_session_id"]))
                    top_sess_id = str(sr["parent_session_id"])
                    conf = "exact_hermes_delegate"
                else:
                    trace_id = make_trace_id("hermes", sid)
                    parent_span_id = None
                    top_sess_id = sid
                    conf = "standalone"

                sess_span_id = make_span_id("hermes", "session", profile_name, sid)
                in_tok = int(sr["input_tokens"] or 0) + int(sr["cache_read_tokens"] or 0)
                out_tok = int(sr["output_tokens"] or 0)

                emitter.add(
                    build_span(
                        trace_id=trace_id,
                        span_id=sess_span_id,
                        name=f"hermes.session [{profile_name}] ({sid[:18]})",
                        span_kind="AGENT",
                        parent_id=parent_span_id,
                        start_time=st_sec,
                        end_time=en_sec,
                        status_code="OK",
                        attributes={
                            "session.id": top_sess_id,
                            "child.session_id": sid,
                            "harness": "hermes",
                            "hermes.profile": profile_name,
                            "agent.name": f"hermes:{profile_name}",
                            "hermes.source": sr["source"] or "",
                            "hermes.created_source": sr["created_source"] or "",
                            "hermes.end_reason": sr["end_reason"] or "",
                            "llm.model_name": sr["model"] or "",
                            "llm.provider": sr["billing_provider"] or "",
                            "llm.token_count.prompt": in_tok,
                            "llm.token_count.completion": out_tok,
                            "llm.token_count.total": in_tok + out_tok,
                            "working_directory": sr["cwd"] or "",
                            "input.value": sr["title"] or "",
                            "correlation.confidence": conf,
                        },
                    )
                )
                spans_count += 1

            if sess_rows:
                store.set_db_watermark("hermes_state_db", db_file, "sessions", last_ts=str(max_sess_ts))

            msg_rows = con.execute(
                """
                SELECT m.id, m.session_id, m.role, m.content, m.tool_call_id, m.tool_calls,
                       m.tool_name, m.timestamp, m.token_count, m.finish_reason, m.reasoning,
                       s.started_at AS sess_started_at, s.parent_session_id, s.model, s.billing_provider
                FROM messages m
                LEFT JOIN sessions s ON s.id = m.session_id
                WHERE m.id > ? AND COALESCE(m.timestamp, s.last_activity_at, 0) >= ?
                ORDER BY m.id ASC
                """,
                (last_msg_id, cutoff_ts),
            ).fetchall()

            max_msg_id = last_msg_id
            for mr in msg_rows:
                mid = int(mr["id"])
                if mid > max_msg_id:
                    max_msg_id = mid
                sid = mr["session_id"] or "unknown"
                mts = float(mr["timestamp"] or mr["sess_started_at"] or cutoff_ts)

                link = store.resolve_session_link("hermes", sid, int(mts * 1000))
                if not link and mr["parent_session_id"]:
                    link = store.resolve_session_link("hermes", str(mr["parent_session_id"]), int(mts * 1000))

                trace_id = link.trace_id if link else (
                    make_trace_id("hermes", str(mr["parent_session_id"]))
                    if mr["parent_session_id"]
                    else make_trace_id("hermes", sid)
                )
                top_sess_id = link.raven_session_key if link else (str(mr["parent_session_id"]) if mr["parent_session_id"] else sid)
                sess_span_id = make_span_id("hermes", "session", profile_name, sid)

                role = mr["role"] or "unknown"
                content = str(mr["content"] or "")
                sp_id = make_span_id("hermes", "msg", profile_name, mid)

                if role == "user":
                    emitter.add(
                        build_span(
                            trace_id=trace_id,
                            span_id=sp_id,
                            name=f"hermes.turn.user [{profile_name}]",
                            span_kind="CHAIN",
                            parent_id=sess_span_id,
                            start_time=mts,
                            end_time=mts,
                            status_code="OK",
                            attributes={
                                "session.id": top_sess_id,
                                "child.session_id": sid,
                                "harness": "hermes",
                                "hermes.profile": profile_name,
                                "input.value": content[:16000],
                            },
                        )
                    )
                    spans_count += 1
                elif role == "assistant":
                    tcs_raw = mr["tool_calls"]
                    tcs = []
                    if tcs_raw:
                        try:
                            tcs = json.loads(tcs_raw) if isinstance(tcs_raw, str) else tcs_raw
                        except Exception:
                            tcs = []
                    tok_cnt = int(mr["token_count"] or 0)
                    emitter.add(
                        build_span(
                            trace_id=trace_id,
                            span_id=sp_id,
                            name=f"hermes.turn.assistant [{profile_name}]",
                            span_kind="LLM",
                            parent_id=sess_span_id,
                            start_time=mts,
                            end_time=mts,
                            status_code="OK",
                            attributes={
                                "session.id": top_sess_id,
                                "child.session_id": sid,
                                "harness": "hermes",
                                "hermes.profile": profile_name,
                                "llm.model_name": mr["model"] or "",
                                "llm.provider": mr["billing_provider"] or "",
                                "llm.token_count.completion": tok_cnt,
                                "output.value": content[:16000],
                                "tool.call_count": len(tcs) if isinstance(tcs, list) else 0,
                            },
                        )
                    )
                    spans_count += 1
                elif role == "tool":
                    tname = mr["tool_name"] or "tool"
                    is_skill = tname in ("skill_view", "skills_list", "read_skill", "use_skill")
                    clower = content.lower()
                    is_tool_err = (
                        "error executing tool" in clower
                        or "returned error" in clower
                        or "timed out after" in clower
                        or '"status": "error"' in clower
                        or "is not a known tool name" in clower
                        or ("skill '" in clower and "not found" in clower)
                        or "traceback (most recent call last)" in clower
                    )
                    if is_tool_err:
                        errors_count += 1
                    emitter.add(
                        build_span(
                            trace_id=trace_id,
                            span_id=sp_id,
                            name=f"hermes.{'skill' if is_skill else 'tool'}.{tname} [{profile_name}]",
                            span_kind="TOOL",
                            parent_id=sess_span_id,
                            start_time=mts,
                            end_time=mts,
                            status_code="ERROR" if is_tool_err else "OK",
                            status_message=content[:500] if is_tool_err else "",
                            attributes={
                                "session.id": top_sess_id,
                                "child.session_id": sid,
                                "harness": "hermes",
                                "hermes.profile": profile_name,
                                "tool.name": tname,
                                "is_skill": is_skill,
                                "output.value": content[:12000],
                                "error.class": "hermes_tool_error" if is_tool_err else None,
                            },
                        )
                    )
                    spans_count += 1

            if max_msg_id > last_msg_id:
                store.set_db_watermark("hermes_state_db", db_file, "messages", last_rowid=max_msg_id)
        finally:
            con.close()

        exec_db = prof_dir / "cron" / "executions.db"
        if exec_db.exists():
            _, last_exec_ts_str = store.get_db_watermark("hermes_cron_exec", exec_db, "executions")
            last_exec_iso = last_exec_ts_str or ensure_utc(cutoff_ts).isoformat()
            try:
                econ = _connect_readonly(exec_db)
                try:
                    erows = econ.execute(
                        """
                        SELECT id, job_id, source, status, claimed_at, started_at, finished_at, error, delivery_outcome, scheduled_instant
                        FROM executions
                        WHERE COALESCE(started_at, claimed_at, '') >= ?
                        ORDER BY COALESCE(started_at, claimed_at, '') ASC
                        """,
                        (last_exec_iso,),
                    ).fetchall()
                    max_exec_iso = last_exec_iso
                    for er in erows:
                        eid = str(er["id"] or "")
                        jid = str(er["job_id"] or "")
                        st_iso = str(er["started_at"] or er["claimed_at"] or "")
                        fn_iso = str(er["finished_at"] or st_iso)
                        if st_iso > max_exec_iso:
                            max_exec_iso = st_iso
                        status = str(er["status"] or "unknown")
                        err_txt = str(er["error"] or "")
                        is_err = status in ("failed", "error", "unknown") or bool(err_txt)
                        err_cls = None
                        if is_err:
                            errors_count += 1
                            if "GatewayUnavailableError" in err_txt:
                                err_cls = "hermes_cron_gateway_unavailable"
                            elif "RuntimeStoreError" in err_txt:
                                err_cls = "hermes_cron_runtime_store_error"
                            elif "owner exited" in err_txt:
                                err_cls = "hermes_cron_owner_exited_unclean"
                            else:
                                err_cls = "hermes_cron_execution_failed"
                        trace_id = make_trace_id("hermes", f"cron:{profile_name}:{jid}")
                        emitter.add(
                            build_span(
                                trace_id=trace_id,
                                span_id=make_span_id("hermes_cron_exec", profile_name, eid),
                                name=f"hermes.cron.execution [{profile_name}] {jid} ({status})",
                                span_kind="AGENT",
                                parent_id=None,
                                start_time=st_iso or cutoff_ts,
                                end_time=fn_iso or st_iso or cutoff_ts,
                                status_code="ERROR" if is_err else "OK",
                                status_message=err_txt[:500] if is_err else "",
                                attributes={
                                    "session.id": f"cron:{jid}",
                                    "harness": "hermes",
                                    "hermes.profile": profile_name,
                                    "agent.name": f"hermes:{profile_name}",
                                    "cron.job_id": jid,
                                    "cron.execution_id": eid,
                                    "cron.source": str(er["source"] or ""),
                                    "cron.status": status,
                                    "cron.delivery_outcome": str(er["delivery_outcome"] or ""),
                                    "cron.scheduled_instant": str(er["scheduled_instant"] or ""),
                                    "error.class": err_cls,
                                    "output.value": err_txt[:4000],
                                },
                            )
                        )
                        spans_count += 1
                    if erows:
                        store.set_db_watermark("hermes_cron_exec", exec_db, "executions", last_ts=max_exec_iso)
                finally:
                    econ.close()
            except Exception:
                pass

    return spans_count, errors_count


def poll_hermes_error_logs(
    store: StateStore,
    emitter: PhoenixEmitter,
    cutoff_ts: float,
) -> tuple[int, int]:
    """Poll errors.log across all discovered Hermes profiles and collapse 5-min OAuth 400 refresh loops into bucketed spans."""
    spans_count = 0
    errors_count = 0

    oauth_buckets: dict[str, dict[str, Any]] = {}

    for profile_name, prof_dir in _PROFILES:
        err_log = prof_dir / "logs" / "errors.log"
        if not err_log.exists():
            continue
        try:
            st = os.stat(err_log)
        except OSError:
            continue
        if st.st_mtime < cutoff_ts:
            continue

        offset, prev_mtime_ns, prev_ctime = store.get_file_watermark(err_log)
        if st.st_size < offset or (prev_ctime and int(st.st_ctime) != prev_ctime):
            offset = 0
        if st.st_size == offset and st.st_mtime_ns == prev_mtime_ns:
            continue

        try:
            with open(err_log, "rb") as bf:
                bf.seek(offset)
                raw = bf.read()
                new_offset = bf.tell()
        except OSError:
            continue

        for idx, line in enumerate(raw.decode("utf-8", errors="replace").splitlines()):
            m = _HERMES_LOG_RE.match(line)
            if not m:
                continue
            ts_str = m.group("ts").replace(",", ".")
            dt = ensure_utc(ts_str)
            ts_epoch = dt.timestamp()
            if ts_epoch < cutoff_ts:
                continue

            level = m.group("level")
            logger = m.group("logger").strip()
            msg = m.group("msg").strip()

            # Check for 5-min MCP OAuth 400 refresh loop or non-interactive MCP OAuth setup warning
            if (
                ("mcp_oauth_manager" in logger and "Token refresh failed:" in msg)
                or ("mcp.client.auth.oauth2" in logger and "OAuth flow error" in msg)
                or ("tools.mcp_tool" in logger and "MCP OAuth setup failed" in msg)
            ):
                # Collapse into 1-hour summary buckets per profile & server so Phoenix isn't flooded,
                # while recording exact 5-minute repeat count inside attributes!
                srv_m = _MCP_SETUP_RE.search(msg)
                mcp_srv = srv_m.group(1) if srv_m else "Consensus"
                # Avoid double-counting the paired WARNING (Token refresh failed: 400) + ERROR (OAuth flow error) 1s apart
                if "OAuth flow error" in msg:
                    continue
                hour_bucket = int(ts_epoch // 3600) * 3600
                bkey = f"hermes:{profile_name}:mcp_oauth_400_loop:{mcp_srv}:{hour_bucket}"
                b = oauth_buckets.get(bkey)
                if not b:
                    b = {
                        "profile": profile_name,
                        "mcp_server": mcp_srv,
                        "window_start": hour_bucket,
                        "count": 0,
                        "first_ts": ts_epoch,
                        "last_ts": ts_epoch,
                        "sample": msg,
                    }
                    oauth_buckets[bkey] = b
                b["count"] += 1
                if ts_epoch < b["first_ts"]:
                    b["first_ts"] = ts_epoch
                if ts_epoch > b["last_ts"]:
                    b["last_ts"] = ts_epoch
                continue

            # Other operational errors in Hermes errors.log
            err_class: Optional[str] = None
            if "exited UNCLEANLY" in msg:
                err_class = "gateway_previous_unclean_exit"
            elif "gateway run refused (exit 75)" in msg:
                err_class = "gateway_elevation_refused_exit75"
            elif "'control-pipe' timed out after" in msg:
                err_class = "control_pipe_timeout_2s"
            elif "Streaming failed before delivery" in msg or "API call failed" in msg:
                err_class = "provider_streaming_or_api_error"
            elif "whatsapp connect timed out" in msg or "whatsapp error" in msg:
                err_class = "whatsapp_connect_error"
            elif "session_read_only" in msg:
                err_class = "session_read_only_refused"
            elif "failed initial connection with a permanent error" in msg:
                err_class = "mcp_trail_permanent_error"
            elif level in ("ERROR", "CRITICAL"):
                err_class = f"hermes_{logger.replace('.', '_')}_error"

            if not err_class:
                continue

            trace_id = make_trace_id("hermes", f"profile:{profile_name}")
            root_id = make_span_id("hermes", "profile_root", profile_name)
            emitter.add(
                build_span(
                    trace_id=trace_id,
                    span_id=make_span_id("hermes_err", profile_name, offset + idx, ts_str),
                    name=f"hermes.error.{err_class} [{profile_name}]",
                    span_kind="CHAIN",
                    parent_id=None,
                    start_time=dt,
                    end_time=dt,
                    status_code="ERROR",
                    status_message=msg[:500],
                    attributes={
                        "session.id": f"hermes:{profile_name}",
                        "harness": "hermes",
                        "hermes.profile": profile_name,
                        "agent.name": f"hermes:{profile_name}",
                        "error.class": err_class,
                        "error.count": 1,
                        "log.level": level,
                        "log.logger": logger,
                        "output.value": msg,
                    },
                )
            )
            spans_count += 1
            errors_count += 1

        store.set_file_watermark("hermes_err", err_log, new_offset, st.st_mtime_ns, int(st.st_ctime))

    # Persist and emit collapsed MCP OAuth 400 refresh loop buckets
    for bkey, b in oauth_buckets.items():
        row = store.conn.execute("SELECT count FROM error_buckets WHERE bucket_key = ?", (bkey,)).fetchone()
        new_count = (int(row["count"]) if row else 0) + b["count"]
        store.conn.execute(
            """
            INSERT INTO error_buckets (
                bucket_key, harness, profile, error_class, window_start,
                count, first_ts, last_ts, sample_message, emitted_count
            ) VALUES (?, 'hermes', ?, 'mcp_oauth_400_refresh_loop', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket_key) DO UPDATE SET
                count = ?,
                last_ts = MAX(error_buckets.last_ts, excluded.last_ts),
                emitted_count = ?
            """,
            (
                bkey,
                b["profile"],
                b["window_start"],
                new_count,
                b["first_ts"],
                b["last_ts"],
                b["sample"],
                new_count,
                new_count,
                new_count,
            ),
        )
        prof = b["profile"]
        trace_id = make_trace_id("hermes", f"profile:{prof}")
        emitter.add(
            build_span(
                trace_id=trace_id,
                span_id=make_span_id("hermes_oauth_bucket", bkey, new_count),
                name=f"hermes.error.mcp_oauth_400_refresh_loop [{prof}/{b['mcp_server']}] (x{new_count})",
                span_kind="CHAIN",
                parent_id=None,
                start_time=b["first_ts"],
                end_time=b["last_ts"],
                status_code="ERROR",
                status_message=f"{new_count}x 5-min MCP OAuth 400 refresh failures on {prof} ({b['mcp_server']}): {b['sample']}",
                attributes={
                    "session.id": f"hermes:{prof}",
                    "harness": "hermes",
                    "hermes.profile": prof,
                    "agent.name": f"hermes:{prof}",
                    "mcp.server": b["mcp_server"],
                    "error.class": "mcp_oauth_400_refresh_loop",
                    "error.count": new_count,
                    "output.value": b["sample"],
                },
            )
        )
        spans_count += 1
        errors_count += b["count"]

    return spans_count, errors_count


class HermesAdapter:
    name = "hermes"

    def discover(self, ctx: Context) -> bool:
        return bool(self._profiles(ctx))

    @staticmethod
    def _profiles(ctx: Context) -> list[tuple[str, Path]]:
        o = ctx.dir_for("hermes")
        return paths.hermes_profiles([o] if o else None)

    def poll(self, ctx: Context, state: StateStore, store: PhoenixEmitter, cutoff_ts: float) -> PollResult:
        global _PROFILES
        _PROFILES = self._profiles(ctx)
        out: PollResult = {}
        try:
            out["hermes_db"] = poll_hermes_state_dbs(state, store, cutoff_ts)
        except Exception as e:
            state.record_source_health("hermes_db", last_error=str(e))
            out["hermes_db"] = (0, 0)
        try:
            out["hermes_err"] = poll_hermes_error_logs(state, store, cutoff_ts)
        except Exception as e:
            state.record_source_health("hermes_err", last_error=str(e))
            out["hermes_err"] = (0, 0)
        return out
