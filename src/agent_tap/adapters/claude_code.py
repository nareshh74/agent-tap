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

_ROOTS: list[Path] = []
def poll_claude(
    store: StateStore,
    emitter: PhoenixEmitter,
    cutoff_ts: float,
) -> tuple[int, int]:
    spans_count = 0
    errors_count = 0
    if not _ROOTS:
        return 0, 0

    for cf in [f for r in _ROOTS for f in glob.glob(str(r / "**" / "*.jsonl"), recursive=True)]:
        try:
            st = os.stat(cf)
        except OSError:
            continue
        if st.st_mtime < cutoff_ts:
            continue

        offset, prev_mtime_ns, prev_ctime = store.get_file_watermark(cf)
        if st.st_size < offset or (prev_ctime and int(st.st_ctime) != prev_ctime):
            offset = 0
        if st.st_size == offset and st.st_mtime_ns == prev_mtime_ns:
            continue

        p = Path(cf)
        is_subagent = p.parent.name == "subagents"
        file_stem = p.stem

        # Inspect header lines to determine sessionId / parentSessionId
        parent_claude_sid: Optional[str] = None
        claude_sid = file_stem
        cwd = ""
        first_ts = st.st_ctime
        try:
            with open(cf, "r", encoding="utf-8", errors="replace") as f:
                for _ in range(5):
                    l0 = f.readline()
                    if not l0:
                        break
                    d0 = json.loads(l0)
                    if d0.get("type") == "fork-context-ref":
                        parent_claude_sid = d0.get("parentSessionId")
                        claude_sid = f"{parent_claude_sid}:subagent:{d0.get('agentId') or file_stem}"
                    if d0.get("sessionId") and not is_subagent:
                        claude_sid = d0["sessionId"]
                    if d0.get("cwd") and not cwd:
                        cwd = d0["cwd"]
                    if d0.get("timestamp") and first_ts == st.st_ctime:
                        first_ts = d0["timestamp"]
        except Exception:
            pass

        lookup_sid = parent_claude_sid if (is_subagent and parent_claude_sid) else claude_sid
        st_dt = ensure_utc(first_ts)
        link = store.resolve_session_link("claude", lookup_sid, int(st_dt.timestamp() * 1000))

        if link:
            trace_id = link.trace_id
            top_sess_id = link.raven_session_key
            if is_subagent and parent_claude_sid:
                parent_span_id = make_span_id("claude", "session", parent_claude_sid)
                conf = "exact_2nd_degree"
            else:
                parent_span_id = link.raven_node_span_id
                conf = link.confidence
        else:
            root_sid = parent_claude_sid if (is_subagent and parent_claude_sid) else claude_sid
            trace_id = make_trace_id("claude", root_sid)
            top_sess_id = root_sid
            parent_span_id = make_span_id("claude", "session", parent_claude_sid) if (is_subagent and parent_claude_sid) else None
            conf = "exact_claude_subagent" if is_subagent else "standalone"

        sess_span_id = make_span_id("claude", "session", claude_sid)
        if offset == 0:
            emitter.add(
                build_span(
                    trace_id=trace_id,
                    span_id=sess_span_id,
                    name=f"claude.{'subagent' if is_subagent else 'session'} ({file_stem[:18]})",
                    span_kind="AGENT",
                    parent_id=parent_span_id,
                    start_time=first_ts,
                    end_time=st.st_mtime,
                    status_code="OK",
                    attributes={
                        "session.id": top_sess_id,
                        "child.session_id": claude_sid,
                        "harness": "claude",
                        "agent.name": "Claude Code Subagent" if is_subagent else "Claude Code",
                        "working_directory": cwd,
                        "correlation.confidence": conf,
                    },
                )
            )
            spans_count += 1

        try:
            with open(cf, "rb") as bf:
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
            rtype = rec.get("type")
            ts = rec.get("timestamp") or st.st_mtime
            uuid = rec.get("uuid") or f"{offset}_{idx}"
            msg = rec.get("message") or {}
            content_list = msg.get("content") if isinstance(msg, dict) else None

            if rtype == "user":
                if isinstance(content_list, list):
                    for c_idx, item in enumerate(content_list):
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "tool_result":
                            is_err = bool(item.get("is_error"))
                            t_out = str(item.get("content") or "")
                            if is_err:
                                errors_count += 1
                            err_cls = None
                            if is_err:
                                err_cls = "claude_sleep_guardrail_blocked" if "Blocked: sleep" in t_out else "claude_tool_error"
                            emitter.add(
                                build_span(
                                    trace_id=trace_id,
                                    span_id=make_span_id("claude", "tool_res", claude_sid, uuid, c_idx),
                                    name="claude.tool_result",
                                    span_kind="TOOL",
                                    parent_id=sess_span_id,
                                    start_time=ts,
                                    end_time=ts,
                                    status_code="ERROR" if is_err else "OK",
                                    status_message=t_out[:500] if is_err else "",
                                    attributes={
                                        "session.id": top_sess_id,
                                        "child.session_id": claude_sid,
                                        "harness": "claude",
                                        "tool.id": item.get("tool_use_id") or "",
                                        "output.value": t_out[:12000],
                                        "error.class": err_cls,
                                    },
                                )
                            )
                            spans_count += 1
                        elif item.get("type") == "text":
                            emitter.add(
                                build_span(
                                    trace_id=trace_id,
                                    span_id=make_span_id("claude", "user", claude_sid, uuid, c_idx),
                                    name="claude.turn.user",
                                    span_kind="CHAIN",
                                    parent_id=sess_span_id,
                                    start_time=ts,
                                    end_time=ts,
                                    status_code="OK",
                                    attributes={
                                        "session.id": top_sess_id,
                                        "child.session_id": claude_sid,
                                        "harness": "claude",
                                        "input.value": str(item.get("text") or "")[:16000],
                                    },
                                )
                            )
                            spans_count += 1
                elif isinstance(msg.get("content"), str):
                    emitter.add(
                        build_span(
                            trace_id=trace_id,
                            span_id=make_span_id("claude", "user", claude_sid, uuid),
                            name="claude.turn.user",
                            span_kind="CHAIN",
                            parent_id=sess_span_id,
                            start_time=ts,
                            end_time=ts,
                            status_code="OK",
                            attributes={
                                "session.id": top_sess_id,
                                "child.session_id": claude_sid,
                                "harness": "claude",
                                "input.value": msg["content"][:16000],
                            },
                        )
                    )
                    spans_count += 1

            elif rtype == "assistant" and isinstance(msg, dict):
                usage = msg.get("usage") or {}
                in_tok = (
                    int(usage.get("input_tokens") or 0)
                    + int(usage.get("cache_creation_input_tokens") or 0)
                    + int(usage.get("cache_read_input_tokens") or 0)
                )
                out_tok = int(usage.get("output_tokens") or 0)
                model_nm = rec.get("requestedModel") or msg.get("model") or ""
                texts = []
                tool_uses = []
                if isinstance(content_list, list):
                    for item in content_list:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                texts.append(str(item.get("text") or ""))
                            elif item.get("type") == "tool_use":
                                tool_uses.append(item)

                asst_span_id = make_span_id("claude", "asst", claude_sid, uuid)
                emitter.add(
                    build_span(
                        trace_id=trace_id,
                        span_id=asst_span_id,
                        name="claude.turn.assistant",
                        span_kind="LLM",
                        parent_id=sess_span_id,
                        start_time=ts,
                        end_time=ts,
                        status_code="OK",
                        attributes={
                            "session.id": top_sess_id,
                            "child.session_id": claude_sid,
                            "harness": "claude",
                            "llm.model_name": model_nm,
                            "llm.token_count.prompt": in_tok,
                            "llm.token_count.completion": out_tok,
                            "llm.token_count.total": in_tok + out_tok,
                            "output.value": "\n".join(texts)[:16000],
                            "tool.call_count": len(tool_uses),
                        },
                    )
                )
                spans_count += 1

                for tu in tool_uses:
                    tname = tu.get("name") or "tool"
                    tid = tu.get("id") or uuid
                    tinput = tu.get("input") or {}
                    is_skill = tname in ("Skill", "read_skill", "use_skill")
                    emitter.add(
                        build_span(
                            trace_id=trace_id,
                            span_id=make_span_id("claude", "tool_use", claude_sid, tid),
                            name=f"claude.{'skill' if is_skill else 'tool'}.{tname}",
                            span_kind="TOOL",
                            parent_id=asst_span_id,
                            start_time=ts,
                            end_time=ts,
                            status_code="OK",
                            attributes={
                                "session.id": top_sess_id,
                                "child.session_id": claude_sid,
                                "harness": "claude",
                                "tool.name": tname,
                                "tool.id": tid,
                                "is_skill": is_skill,
                                "input.value": json.dumps(tinput, ensure_ascii=False)[:8000],
                            },
                        )
                    )
                    spans_count += 1

        store.set_file_watermark("claude", cf, new_offset, st.st_mtime_ns, int(st.st_ctime))

    return spans_count, errors_count


# ============================================================================


class ClaudeCodeAdapter:
    name = "claude-code"

    def discover(self, ctx: Context) -> bool:
        return bool(self._roots(ctx))

    @staticmethod
    def _roots(ctx: Context) -> list[Path]:
        o = ctx.dir_for("claude-code")
        return [o] if o else paths.claude_roots()

    def poll(self, ctx: Context, state: StateStore, store: PhoenixEmitter, cutoff_ts: float) -> PollResult:
        global _ROOTS
        _ROOTS = self._roots(ctx)
        out: PollResult = {}
        try:
            out["claude"] = poll_claude(state, store, cutoff_ts)
        except Exception as e:
            state.record_source_health("claude", last_error=str(e))
            out["claude"] = (0, 0)
        return out
