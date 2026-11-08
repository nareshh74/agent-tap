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


def _load_flat_yaml(path: Path) -> dict[str, Any]:
    """Minimal 'key: value' reader for Copilot workspace.yaml (avoids a PyYAML dependency)."""
    out: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^([A-Za-z_][\w.-]*):\s*(.*)$", line)
        if m:
            v = m.group(2).strip().strip("'\"")
            out[m.group(1)] = v if v not in ("", "null", "~") else None
    return out

def poll_copilot(
    store: StateStore,
    emitter: PhoenixEmitter,
    cutoff_ts: float,
) -> tuple[int, int]:
    spans_count = 0
    errors_count = 0

    sess_state_dir = _ROOT / "session-state"
    if sess_state_dir.exists():
        for entry in os.scandir(sess_state_dir):
            if not entry.is_dir():
                continue
            sid = entry.name
            ws_file = Path(entry.path) / "workspace.yaml"
            ev_file = Path(entry.path) / "events.jsonl"

            target_stat_file = ev_file if ev_file.exists() else ws_file
            if not target_stat_file.exists():
                continue
            try:
                st = target_stat_file.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff_ts:
                continue

            ws_meta: dict[str, Any] = {}
            if ws_file.exists():
                try:
                    ws_meta = _load_flat_yaml(ws_file) or {}
                except Exception:
                    ws_meta = {}

            created_at = ws_meta.get("created_at") or st.st_ctime
            updated_at = ws_meta.get("updated_at") or st.st_mtime
            cwd = ws_meta.get("cwd") or ""
            client_name = ws_meta.get("client_name") or "github/cli"
            sess_title = ws_meta.get("name") or sid

            st_dt = ensure_utc(created_at)
            link = store.resolve_session_link("copilot", sid, int(st_dt.timestamp() * 1000))
            trace_id = link.trace_id if link else make_trace_id("copilot", sid)
            parent_span_id = link.raven_node_span_id if link else None
            top_sess_id = link.raven_session_key if link else sid
            sess_span_id = make_span_id("copilot", "session", sid)

            # Check workspace.yaml watermark for short-lived ACP stdio sessions without events.jsonl
            if not ev_file.exists():
                _, prev_mtime_ns, _ = store.get_file_watermark(ws_file)
                if st.st_mtime_ns != prev_mtime_ns:
                    emitter.add(
                        build_span(
                            trace_id=trace_id,
                            span_id=sess_span_id,
                            name=f"copilot.session [{client_name}] ({sid[:8]})",
                            span_kind="AGENT",
                            parent_id=parent_span_id,
                            start_time=created_at,
                            end_time=updated_at,
                            status_code="OK",
                            attributes={
                                "session.id": top_sess_id,
                                "child.session_id": sid,
                                "harness": "copilot",
                                "agent.name": "GitHub Copilot",
                                "copilot.client_name": client_name,
                                "working_directory": str(cwd),
                                "input.value": str(sess_title)[:4000],
                                "correlation.confidence": link.confidence if link else "standalone",
                            },
                        )
                    )
                    spans_count += 1
                    store.set_file_watermark("copilot_ws", ws_file, st.st_size, st.st_mtime_ns, int(st.st_ctime))
                continue

            offset, prev_mtime_ns, prev_ctime = store.get_file_watermark(ev_file)
            if st.st_size < offset or (prev_ctime and int(st.st_ctime) != prev_ctime):
                offset = 0
            if st.st_size == offset and st.st_mtime_ns == prev_mtime_ns:
                continue

            if offset == 0:
                emitter.add(
                    build_span(
                        trace_id=trace_id,
                        span_id=sess_span_id,
                        name=f"copilot.session [{client_name}] ({sid[:8]})",
                        span_kind="AGENT",
                        parent_id=parent_span_id,
                        start_time=created_at,
                        end_time=updated_at,
                        status_code="OK",
                        attributes={
                            "session.id": top_sess_id,
                            "child.session_id": sid,
                            "harness": "copilot",
                            "agent.name": "GitHub Copilot",
                            "copilot.client_name": client_name,
                            "working_directory": str(cwd),
                            "input.value": str(sess_title)[:4000],
                            "correlation.confidence": link.confidence if link else "standalone",
                        },
                    )
                )
                spans_count += 1

            try:
                with open(ev_file, "rb") as bf:
                    bf.seek(offset)
                    raw = bf.read()
                    new_offset = bf.tell()
            except OSError:
                continue

            for idx, line in enumerate(raw.decode("utf-8", errors="replace").splitlines()):
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                ev_type = ev.get("type") or ""
                ev_id = ev.get("id") or f"{offset}_{idx}"
                ts = ev.get("timestamp") or updated_at
                data = ev.get("data") or {}
                sp_id = make_span_id("copilot", "ev", sid, ev_id)

                if ev_type == "user.message":
                    emitter.add(
                        build_span(
                            trace_id=trace_id,
                            span_id=sp_id,
                            name="copilot.turn.user",
                            span_kind="CHAIN",
                            parent_id=sess_span_id,
                            start_time=ts,
                            end_time=ts,
                            status_code="OK",
                            attributes={
                                "session.id": top_sess_id,
                                "child.session_id": sid,
                                "harness": "copilot",
                                "input.value": str(data.get("content") or data.get("message") or data)[:12000],
                            },
                        )
                    )
                    spans_count += 1
                elif ev_type == "assistant.message":
                    model_nm = data.get("model") or ""
                    emitter.add(
                        build_span(
                            trace_id=trace_id,
                            span_id=sp_id,
                            name="copilot.turn.assistant",
                            span_kind="LLM",
                            parent_id=sess_span_id,
                            start_time=ts,
                            end_time=ts,
                            status_code="OK",
                            attributes={
                                "session.id": top_sess_id,
                                "child.session_id": sid,
                                "harness": "copilot",
                                "llm.model_name": str(model_nm),
                                "output.value": str(data.get("content") or data.get("message") or data)[:12000],
                            },
                        )
                    )
                    spans_count += 1
                elif ev_type in ("tool.execution_start", "tool.execution_complete"):
                    tname = data.get("toolName") or data.get("name") or "tool"
                    is_skill = "skill" in tname.lower()
                    is_err = data.get("success") is False or bool(data.get("error"))
                    if ev_type == "tool.execution_start" and not is_err:
                        # Emit on complete if available; still emit start if no complete
                        pass
                    if is_err:
                        errors_count += 1
                    err_msg = ""
                    if isinstance(data.get("error"), dict):
                        err_msg = str(data["error"].get("message") or data["error"])
                    elif data.get("error"):
                        err_msg = str(data.get("error"))
                    emitter.add(
                        build_span(
                            trace_id=trace_id,
                            span_id=sp_id,
                            name=f"copilot.{'skill' if is_skill else 'tool'}.{tname} ({ev_type.split('.')[-1]})",
                            span_kind="TOOL",
                            parent_id=sess_span_id,
                            start_time=ts,
                            end_time=ts,
                            status_code="ERROR" if is_err else "OK",
                            status_message=err_msg[:500],
                            attributes={
                                "session.id": top_sess_id,
                                "child.session_id": sid,
                                "harness": "copilot",
                                "tool.name": tname,
                                "is_skill": is_skill,
                                "input.value": str(data.get("arguments") or data.get("input") or "")[:8000],
                                "output.value": str(data.get("result") or err_msg or "")[:8000],
                                "error.class": "copilot_tool_error" if is_err else None,
                            },
                        )
                    )
                    spans_count += 1
                elif ev_type in ("hook.end", "abort"):
                    is_err = ev_type == "abort" or bool(data.get("error")) or "Timeout" in str(data)
                    if not is_err:
                        continue
                    errors_count += 1
                    err_cls = "copilot_hook_timeout" if ev_type == "hook.end" else "copilot_abort"
                    emitter.add(
                        build_span(
                            trace_id=trace_id,
                            span_id=sp_id,
                            name=f"copilot.error.{err_cls}",
                            span_kind="CHAIN",
                            parent_id=sess_span_id,
                            start_time=ts,
                            end_time=ts,
                            status_code="ERROR",
                            status_message=str(data)[:500],
                            attributes={
                                "session.id": top_sess_id,
                                "child.session_id": sid,
                                "harness": "copilot",
                                "error.class": err_cls,
                                "output.value": json.dumps(data, ensure_ascii=False),
                            },
                        )
                    )
                    spans_count += 1

            store.set_file_watermark("copilot_event", ev_file, new_offset, st.st_mtime_ns, int(st.st_ctime))

    # Tail Copilot process-*.log files and correlate to session UUID
    logs_dir = _ROOT / "logs"
    if logs_dir.exists():
        for plog in glob.glob(str(logs_dir / "process-*.log")):
            try:
                st = os.stat(plog)
            except OSError:
                continue
            if st.st_mtime < cutoff_ts:
                continue
            offset, prev_mtime_ns, prev_ctime = store.get_file_watermark(plog)
            if st.st_size < offset or (prev_ctime and int(st.st_ctime) != prev_ctime):
                offset = 0
            if st.st_size == offset and st.st_mtime_ns == prev_mtime_ns:
                continue

            # Discover session UUID from full file first if needed
            full_text = Path(plog).read_text(encoding="utf-8", errors="replace")
            sess_uuid: Optional[str] = None
            for line in full_text.splitlines():
                if "for session " in line or "session id " in line:
                    um = _UUID_RE.search(line)
                    if um:
                        sess_uuid = um.group(1)
                        break

            try:
                with open(plog, "rb") as bf:
                    bf.seek(offset)
                    raw = bf.read()
                    new_offset = bf.tell()
            except OSError:
                continue

            for idx, line in enumerate(raw.decode("utf-8", errors="replace").splitlines()):
                m = _COPILOT_LOG_RE.match(line)
                if not m:
                    continue
                ts_str = m.group("ts")
                dt = ensure_utc(ts_str)
                if dt.timestamp() < cutoff_ts:
                    continue
                level = m.group("level")
                msg = m.group("msg")

                err_class: Optional[str] = None
                if "MCP session host is disposed" in msg or "session changed during MCP reload" in msg:
                    err_class = "acp_mcp_disposal_race"
                elif 'MCP server connection failed {"server_name":"trail"' in msg:
                    err_class = "mcp_trail_spawn_failed"
                elif "Failed to scan skill directory" in msg:
                    err_class = "missing_skill_directory"
                elif level == "ERROR":
                    err_class = "copilot_process_error"

                if not err_class:
                    continue

                sid = sess_uuid or Path(plog).stem
                link = store.resolve_session_link("copilot", sid, int(dt.timestamp() * 1000)) if sess_uuid else None
                trace_id = link.trace_id if link else make_trace_id("copilot", sid)
                parent_id = make_span_id("copilot", "session", sid) if sess_uuid else None
                top_sess_id = link.raven_session_key if link else sid

                emitter.add(
                    build_span(
                        trace_id=trace_id,
                        span_id=make_span_id("copilot_log", Path(plog).name, offset + idx),
                        name=f"copilot.error.{err_class}",
                        span_kind="CHAIN",
                        parent_id=parent_id,
                        start_time=dt,
                        end_time=dt,
                        status_code="ERROR",
                        status_message=msg[:500],
                        attributes={
                            "session.id": top_sess_id,
                            "child.session_id": sid,
                            "harness": "copilot",
                            "agent.name": "GitHub Copilot",
                            "error.class": err_class,
                            "log.level": level,
                            "log.file": Path(plog).name,
                            "output.value": msg,
                        },
                    )
                )
                spans_count += 1
                errors_count += 1

            store.set_file_watermark("copilot_log", plog, new_offset, st.st_mtime_ns, int(st.st_ctime))

    return spans_count, errors_count


# ============================================================================


class CopilotCliAdapter:
    name = "copilot-cli"

    def discover(self, ctx: Context) -> bool:
        return self._root(ctx).is_dir()

    @staticmethod
    def _root(ctx: Context) -> Path:
        return ctx.dir_for("copilot-cli", paths.copilot_root())

    def poll(self, ctx: Context, state: StateStore, store: PhoenixEmitter, cutoff_ts: float) -> PollResult:
        global _ROOT
        _ROOT = self._root(ctx)
        out: PollResult = {}
        try:
            out["copilot"] = poll_copilot(state, store, cutoff_ts)
        except Exception as e:
            state.record_source_health("copilot", last_error=str(e))
            out["copilot"] = (0, 0)
        return out
