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
def map_agent_to_child_harness(agent_name: str, agent_info_name: Optional[str] = None) -> str:
    ag = (agent_name or "").strip().lower()
    info = (agent_info_name or "").strip().lower()
    if ag == "raven-general":
        return "raven-general"
    if ag.startswith("raven-") or info == "raven":
        return "raven-acp"
    if "codex" in ag or "codex" in info:
        return "codex"
    if "claude" in ag or "claude" in info:
        return "claude"
    if "copilot" in ag or "copilot" in info:
        return "copilot"
    return "hermes"


def scan_acp_frames_for_session_id(acp_path: str | Path) -> Optional[str]:
    p = Path(acp_path)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 200:
                    break
                if '"session"' in line or '"sessionId"' in line:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("session"):
                        return str(d["session"])
                    frame = d.get("frame") or {}
                    params = frame.get("params") or {}
                    result = frame.get("result") or {}
                    sid = params.get("sessionId") or result.get("sessionId")
                    if sid:
                        return str(sid)
    except Exception:
        pass
    return None


def poll_raven_nodes(
    store: StateStore,
    emitter: PhoenixEmitter,
    cutoff_ts: float,
) -> tuple[int, int]:
    """Poll subagents/nodes/*.meta.json, upsert session_links, and emit subagent node spans."""
    spans_count = 0
    errors_count = 0
    pattern = str(_ROOT / "workspace" / "sessions" / "**" / "subagents" / "nodes" / "*.meta.json")
    for mf in glob.glob(pattern, recursive=True):
        try:
            st = os.stat(mf)
        except OSError:
            continue
        if st.st_mtime < cutoff_ts:
            continue
        _, prev_mtime_ns, _ = store.get_file_watermark(mf)
        if st.st_mtime_ns == prev_mtime_ns:
            continue

        try:
            data = json.loads(Path(mf).read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue

        session_key = data.get("session_key") or "unknown"
        call_id = data.get("call_id") or Path(mf).stem.replace(".meta", "")
        agent = data.get("agent") or "unknown"
        instance = data.get("instance") or ""
        status = data.get("status") or "unknown"
        started_ms = data.get("started_at_ms")
        ended_ms = data.get("ended_at_ms") or (started_ms + 1000 if started_ms else int(st.st_mtime * 1000))

        trace_id = make_trace_id("raven", session_key)
        root_span_id = make_span_id("raven", "root", session_key)
        node_span_id = make_span_id("raven", "node", session_key, call_id)

        acpf = data.get("acp_frames") or {}
        acp_sid = acpf.get("session_id")
        agent_info = ((acpf.get("initialize") or {}).get("agentInfo") or {}).get("name")
        child_harness = map_agent_to_child_harness(agent, agent_info)

        if not acp_sid and acpf.get("path"):
            acp_sid = scan_acp_frames_for_session_id(acpf["path"])

        if acp_sid:
            store.upsert_session_link(
                SessionLink(
                    child_harness=child_harness,
                    child_session_id=str(acp_sid),
                    raven_session_key=session_key,
                    raven_call_id=call_id,
                    raven_node_span_id=node_span_id,
                    trace_id=trace_id,
                    started_at_ms=started_ms,
                    ended_at_ms=ended_ms,
                    confidence="exact",
                )
            )
        elif child_harness == "raven-general" and instance:
            store.upsert_session_link(
                SessionLink(
                    child_harness="raven-general",
                    child_session_id=f"{session_key}#Raven-General/{instance}",
                    raven_session_key=session_key,
                    raven_call_id=call_id,
                    raven_node_span_id=node_span_id,
                    trace_id=trace_id,
                    started_at_ms=started_ms,
                    ended_at_ms=ended_ms,
                    confidence="exact",
                )
            )

        # Ensure root trace span exists for the parent Raven session
        root_span = build_span(
            trace_id=trace_id,
            span_id=root_span_id,
            name=f"raven.session ({session_key})",
            span_kind="AGENT",
            parent_id=None,
            start_time=started_ms or st.st_mtime,
            end_time=ended_ms or st.st_mtime,
            status_code="OK",
            attributes={
                "session.id": session_key,
                "harness": "raven",
                "agent.name": "raven",
                "raven.session_key": session_key,
            },
        )
        emitter.add(root_span)

        tokens_in = int(data.get("tokens_in") or 0)
        tokens_out = int(data.get("tokens_out") or 0)
        quota_tc = ((data.get("acp_response_meta") or {}).get("quota") or {}).get("token_count") or {}
        if not tokens_in and quota_tc.get("inputTokens"):
            tokens_in = int(quota_tc.get("inputTokens") or 0) + int(quota_tc.get("cachedInputTokens") or 0)
        if not tokens_out and quota_tc.get("outputTokens"):
            tokens_out = int(quota_tc.get("outputTokens") or 0)

        tool_calls = data.get("tool_calls") or []
        tool_failures = data.get("tool_failures") or []
        is_err = status in ("failed", "error")
        if is_err:
            errors_count += 1

        # Read output preview if .out.md exists
        out_md_path = Path(mf).with_name(f"{call_id}.out.md")
        out_text = ""
        if out_md_path.exists():
            try:
                out_text = out_md_path.read_text(encoding="utf-8", errors="replace")[:8000]
            except Exception:
                pass

        err_class = "subagent_node_failed" if is_err else None
        if "[raven] The sub-agent used all 15 tool rounds" in out_text:
            err_class = "subagent_max_tool_rounds_15"

        node_span = build_span(
            trace_id=trace_id,
            span_id=node_span_id,
            name=f"subagent.node [{agent}] {call_id}",
            span_kind="AGENT",
            parent_id=root_span_id,
            start_time=started_ms or st.st_mtime,
            end_time=ended_ms or st.st_mtime,
            status_code="ERROR" if is_err else "OK",
            status_message=f"Subagent {agent} ({call_id}) status={status}" if is_err else "",
            attributes={
                "session.id": session_key,
                "harness": "raven",
                "agent.name": agent,
                "child.harness": child_harness,
                "child.session_id": acp_sid or "",
                "raven.call_id": call_id,
                "raven.instance": instance,
                "raven.working_directory": data.get("working_directory") or "",
                "raven.status": status,
                "input.value": data.get("task_summary") or call_id,
                "output.value": out_text,
                "llm.token_count.prompt": tokens_in,
                "llm.token_count.completion": tokens_out,
                "llm.token_count.total": tokens_in + tokens_out,
                "tool.call_count": len(tool_calls),
                "tool.failure_count": len(tool_failures),
                "error.class": err_class,
                "correlation.confidence": "exact" if acp_sid else ("exact" if child_harness == "raven-general" else "unlinked"),
            },
        )
        emitter.add(node_span)
        spans_count += 2

        store.set_file_watermark("raven_node", mf, st.st_size, st.st_mtime_ns, int(st.st_ctime))
    return spans_count, errors_count


def poll_raven_sessions_and_subagents(
    store: StateStore,
    emitter: PhoenixEmitter,
    cutoff_ts: float,
) -> tuple[int, int]:
    """Poll Raven top-level sessions, Raven-General instances, and Native Raven ACP child sessions."""
    spans_count = 0
    errors_count = 0

    session_files: list[tuple[str, str]] = []
    for p in glob.glob(str(_ROOT / "workspace" / "sessions" / "*" / "*.jsonl")):
        session_files.append(("raven", p))
    hb_dot_file = _ROOT / "workspace" / "sessions" / "heartbeat" / ".jsonl"
    if hb_dot_file.exists():
        session_files.append(("raven", str(hb_dot_file)))
    for p in glob.glob(str(_ROOT / "workspace" / "sessions" / "*" / "subagents" / "instances" / "Raven-General" / "*.jsonl")):
        session_files.append(("raven-general", p))
    for p in glob.glob(str(_ROOT / "subagent_sessions" / "*" / "acp" / "sessions" / "*" / "*.jsonl")):
        session_files.append(("raven-acp", p))

    for harness_kind, fp in session_files:
        try:
            st = os.stat(fp)
        except OSError:
            continue
        if st.st_mtime < cutoff_ts:
            continue
        offset, prev_mtime_ns, prev_ctime = store.get_file_watermark(fp)
        if st.st_size < offset or (prev_ctime and int(st.st_ctime) != prev_ctime):
            offset = 0
        if st.st_size == offset and st.st_mtime_ns == prev_mtime_ns:
            continue

        # Read first line for session metadata
        session_key = Path(fp).stem
        root_session_key_hint = None
        model_name = ""
        provider_name = ""
        workdir = ""
        created_at = st.st_ctime
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                first_line = f.readline()
                if first_line:
                    m0 = json.loads(first_line)
                    if m0.get("_type") == "metadata":
                        session_key = m0.get("key") or session_key
                        created_at = m0.get("created_at") or created_at
                        meta = m0.get("metadata") or {}
                        model_name = meta.get("model") or ""
                        provider_name = meta.get("provider") or ""
                        workdir = meta.get("workdir") or meta.get("project_dir") or ""
                        uo = meta.get("usage_owner") or {}
                        root_session_key_hint = uo.get("root_session_key") or meta.get("session_key")
        except Exception:
            pass

        # Resolve parent link if this is a child session
        link: Optional[SessionLink] = None
        if harness_kind in ("raven-acp", "raven-general"):
            link = store.resolve_session_link(harness_kind, session_key)
            if not link and root_session_key_hint:
                trace_id = make_trace_id("raven", root_session_key_hint)
                parent_span_id = make_span_id("raven", "root", root_session_key_hint)
            elif link:
                trace_id = link.trace_id
                parent_span_id = link.raven_node_span_id
            else:
                trace_id = make_trace_id(harness_kind, session_key)
                parent_span_id = None
        else:
            trace_id = make_trace_id("raven", session_key)
            parent_span_id = None

        sess_span_id = make_span_id(harness_kind, "session", session_key)
        if offset == 0:
            emitter.add(
                build_span(
                    trace_id=trace_id,
                    span_id=sess_span_id if harness_kind != "raven" else make_span_id("raven", "root", session_key),
                    name=f"{harness_kind}.session ({session_key})",
                    span_kind="AGENT",
                    parent_id=parent_span_id,
                    start_time=created_at,
                    end_time=st.st_mtime,
                    status_code="OK",
                    attributes={
                        "session.id": link.raven_session_key if link else session_key,
                        "child.session_id": session_key if harness_kind != "raven" else "",
                        "harness": "raven" if harness_kind != "raven-acp" else "raven-acp",
                        "agent.name": Path(fp).parts[-4] if harness_kind == "raven-acp" else harness_kind,
                        "llm.model_name": model_name,
                        "llm.provider": provider_name,
                        "working_directory": workdir,
                        "correlation.confidence": link.confidence if link else ("root" if harness_kind == "raven" else "heuristic"),
                    },
                )
            )
            spans_count += 1

        effective_parent_id = sess_span_id if harness_kind != "raven" else make_span_id("raven", "root", session_key)

        try:
            with open(fp, "rb") as bf:
                bf.seek(offset)
                raw_chunk = bf.read()
                new_offset = bf.tell()
        except OSError:
            continue

        lines = raw_chunk.decode("utf-8", errors="replace").splitlines()
        for idx, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict) or rec.get("_type") == "metadata":
                continue

            role = rec.get("role")
            ts = rec.get("timestamp") or st.st_mtime
            try:
                if ensure_utc(ts).timestamp() < cutoff_ts:
                    continue
            except Exception:
                pass
            content = rec.get("content") or ""
            if isinstance(content, list):
                content = json.dumps(content, ensure_ascii=False)
            line_pos = offset + idx

            if role == "user":
                sp_id = make_span_id(harness_kind, session_key, "user", line_pos)
                emitter.add(
                    build_span(
                        trace_id=trace_id,
                        span_id=sp_id,
                        name=f"{harness_kind}.turn.user",
                        span_kind="CHAIN",
                        parent_id=effective_parent_id,
                        start_time=ts,
                        end_time=ts,
                        status_code="OK",
                        attributes={
                            "session.id": link.raven_session_key if link else session_key,
                            "harness": "raven" if harness_kind == "raven" else harness_kind,
                            "input.value": str(content)[:16000],
                            "raven.trace_id": rec.get("trace_id") or "",
                        },
                    )
                )
                spans_count += 1
            elif role == "assistant":
                sp_id = make_span_id(harness_kind, session_key, "assistant", line_pos)
                text_str = str(content)
                is_err = (
                    "Error calling LLM (first_byte_timeout)" in text_str
                    or "MidStreamFallbackError" in text_str
                    or "used all 15 tool rounds before finishing" in text_str
                )
                err_class = None
                if "first_byte_timeout" in text_str:
                    err_class = "first_byte_timeout"
                elif "MidStreamFallbackError" in text_str:
                    err_class = "litellm_midstream_fallback_error"
                elif "used all 15 tool rounds" in text_str:
                    err_class = "subagent_max_tool_rounds_15"
                if is_err:
                    errors_count += 1

                tcs = rec.get("tool_calls") or []
                emitter.add(
                    build_span(
                        trace_id=trace_id,
                        span_id=sp_id,
                        name=f"{harness_kind}.turn.assistant",
                        span_kind="LLM",
                        parent_id=effective_parent_id,
                        start_time=ts,
                        end_time=ts,
                        status_code="ERROR" if is_err else "OK",
                        status_message=text_str[:500] if is_err else "",
                        attributes={
                            "session.id": link.raven_session_key if link else session_key,
                            "harness": "raven" if harness_kind == "raven" else harness_kind,
                            "llm.model_name": model_name,
                            "llm.provider": provider_name,
                            "output.value": text_str[:16000],
                            "tool.call_count": len(tcs),
                            "error.class": err_class,
                        },
                    )
                )
                spans_count += 1
                for tc in tcs:
                    fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                    t_name = fn.get("name") or "tool"
                    t_args = fn.get("arguments") or ""
                    tc_id = tc.get("id") or f"{line_pos}_{t_name}"
                    is_skill = t_name in ("read_skill", "use_skill", "find_skill", "skill_view")
                    emitter.add(
                        build_span(
                            trace_id=trace_id,
                            span_id=make_span_id(harness_kind, session_key, "tool", tc_id),
                            name=f"skill.{t_name}" if is_skill else f"tool.{t_name}",
                            span_kind="TOOL",
                            parent_id=sp_id,
                            start_time=ts,
                            end_time=ts,
                            status_code="OK",
                            attributes={
                                "session.id": link.raven_session_key if link else session_key,
                                "harness": "raven" if harness_kind == "raven" else harness_kind,
                                "tool.name": t_name,
                                "tool.parameters": str(t_args)[:8000],
                                "input.value": str(t_args)[:8000],
                                "is_skill": is_skill,
                            },
                        )
                    )
                    spans_count += 1
            elif role == "tool":
                t_str = str(content)
                err_cls = None
                if "requires user approval (review timed out" in t_str or "turn is not interactive" in t_str:
                    err_cls = "scheduled_approval_timeout_10s"
                elif "cannot schedule new jobs from within a cron" in t_str:
                    err_cls = "cron_nested_schedule_blocked"
                elif "prior safety decision terminated this action" in t_str:
                    err_cls = "safety_decision_terminated"
                if err_cls:
                    errors_count += 1
                    emitter.add(
                        build_span(
                            trace_id=trace_id,
                            span_id=make_span_id(harness_kind, session_key, "tool_err", line_pos),
                            name=f"raven.error.{err_cls}",
                            span_kind="TOOL",
                            parent_id=effective_parent_id,
                            start_time=ts,
                            end_time=ts,
                            status_code="ERROR",
                            status_message=t_str[:500],
                            attributes={
                                "session.id": link.raven_session_key if link else session_key,
                                "harness": "raven" if harness_kind == "raven" else harness_kind,
                                "tool.name": rec.get("name") or "tool",
                                "error.class": err_cls,
                                "output.value": t_str[:8000],
                            },
                        )
                    )
                    spans_count += 1

        store.set_file_watermark(f"raven_{harness_kind}", fp, new_offset, st.st_mtime_ns, int(st.st_ctime))

    return spans_count, errors_count


def poll_raven_audit_spans(
    store: StateStore,
    emitter: PhoenixEmitter,
    cutoff_ts: float,
) -> tuple[int, int]:
    """Poll Raven's structured audit-spans.log for exact LLM tokens, latency, skills, and subagent links."""
    spans_count = 0
    errors_count = 0
    audit_files = [_ROOT / "traces" / "logs" / "audit-spans.log"]
    for p in glob.glob(str(_ROOT / "traces" / "logs" / "archive" / "*" / "audit-spans-*.log")):
        audit_files.append(Path(p))

    for af in audit_files:
        if not af.exists():
            continue
        try:
            st = os.stat(af)
        except OSError:
            continue
        if st.st_mtime < cutoff_ts:
            continue
        offset, prev_mtime_ns, prev_ctime = store.get_file_watermark(af)
        if st.st_size < offset or (prev_ctime and int(st.st_ctime) != prev_ctime):
            offset = 0
        if st.st_size == offset and st.st_mtime_ns == prev_mtime_ns:
            continue

        try:
            with open(af, "rb") as bf:
                bf.seek(offset)
                raw = bf.read()
                new_offset = bf.tell()
        except OSError:
            continue

        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                sp = json.loads(line)
            except Exception:
                continue
            if not isinstance(sp, dict):
                continue
            attrs = sp.get("attributes") or {}
            if attrs.get("turn.in_progress") is True or attrs.get("subagent.in_progress") is True or attrs.get("subagent.external.in_progress") is True:
                continue

            end_str = sp.get("endTime") or sp.get("startTime")
            end_dt = ensure_utc(end_str)
            if end_dt.timestamp() < cutoff_ts:
                continue

            sess_key = attrs.get("session.key") or attrs.get("session.id") or "raven:system"
            trace_id = make_trace_id("raven", sess_key)
            root_span_id = make_span_id("raven", "root", sess_key)
            raw_span_id = sp.get("spanId") or f"{sp.get('name')}:{sp.get('startTime')}"
            span_id = make_span_id("raven", "audit", raw_span_id)
            parent_raw = sp.get("parentSpanId")
            parent_id = make_span_id("raven", "audit", parent_raw) if parent_raw else root_span_id

            name = sp.get("name") or "raven.span"
            status_obj = sp.get("status") or {}
            status_code = status_obj.get("code") or "OK"
            status_msg = status_obj.get("message") or ""

            if name == "subagent.external":
                ext_sid = attrs.get("subagent.external.session_id") or attrs.get("subagent.external.frames.session_id")
                ext_agent = attrs.get("subagent.external.agent") or ""
                task_id = attrs.get("subagent.external.task_id") or raw_span_id
                if ext_sid:
                    ch_harness = map_agent_to_child_harness(ext_agent)
                    store.upsert_session_link(
                        SessionLink(
                            child_harness=ch_harness,
                            child_session_id=str(ext_sid),
                            raven_session_key=sess_key,
                            raven_call_id=str(task_id),
                            raven_node_span_id=span_id,
                            trace_id=trace_id,
                            started_at_ms=int(ensure_utc(sp.get("startTime")).timestamp() * 1000),
                            ended_at_ms=int(end_dt.timestamp() * 1000),
                            confidence="exact",
                        )
                    )

            kind_map = {
                "llm.call": "LLM",
                "tool.call": "TOOL",
                "skill.inject": "CHAIN",
                "skill.read": "TOOL",
                "memory.recall": "RETRIEVER",
                "subagent.run": "AGENT",
                "subagent.external": "AGENT",
                "session.turn": "CHAIN",
            }
            span_kind = kind_map.get(name, "CHAIN")

            in_tok = int(attrs.get("llm.usage.input_tokens") or 0) + int(attrs.get("llm.usage.cache_read_tokens") or 0)
            out_tok = int(attrs.get("llm.usage.output_tokens") or 0)
            tool_err = attrs.get("tool.error")
            if tool_err or status_code == "ERROR":
                status_code = "ERROR"
                errors_count += 1

            out_attrs: dict[str, Any] = {
                "session.id": sess_key,
                "harness": "raven",
                "raven.audit_span_name": name,
                "raven.trace_id": sp.get("traceId") or "",
            }
            if name == "llm.call":
                out_attrs.update(
                    {
                        "llm.model_name": attrs.get("llm.model") or "",
                        "llm.provider": attrs.get("llm.provider") or "",
                        "llm.token_count.prompt": in_tok,
                        "llm.token_count.completion": out_tok,
                        "llm.token_count.total": in_tok + out_tok,
                        "llm.response_id": attrs.get("llm.response_id") or "",
                        "output.value": attrs.get("llm.output_preview") or "",
                    }
                )
            elif name == "tool.call":
                tname = attrs.get("tool.name") or "tool"
                name = f"tool.{tname}"
                out_attrs.update(
                    {
                        "tool.name": tname,
                        "input.value": attrs.get("tool.args_preview") or "",
                        "output.value": attrs.get("tool.result_preview") or "",
                        "tool.duration_ms": attrs.get("tool.duration_ms"),
                        "error.class": "tool_error" if tool_err else None,
                    }
                )
            elif name in ("skill.inject", "skill.read"):
                out_attrs.update(
                    {
                        "skill.names": attrs.get("skill.inject.names") or attrs.get("skill.name") or [],
                        "skill.count": attrs.get("skill.inject.count") or 1,
                    }
                )
            elif name == "session.turn":
                out_attrs.update(
                    {
                        "input.value": attrs.get("turn.input_preview") or "",
                        "output.value": attrs.get("turn.output_preview") or "",
                    }
                )

            emitter.add(
                build_span(
                    trace_id=trace_id,
                    span_id=span_id,
                    name=name,
                    span_kind=span_kind,
                    parent_id=parent_id,
                    start_time=sp.get("startTime"),
                    end_time=end_dt,
                    status_code=status_code,
                    status_message=str(tool_err or status_msg)[:500],
                    attributes=out_attrs,
                )
            )
            spans_count += 1

        store.set_file_watermark("raven_audit", af, new_offset, st.st_mtime_ns, int(st.st_ctime))

    return spans_count, errors_count


_RAVEN_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s*\|\s*(?P<level>[A-Z]+)\s*\|\s*(?P<loc>[^\s]+)\s*-\s*(?P<msg>.*)$"
)
_SESSION_KEY_RE = re.compile(r"\b((?:tui|acp|cron|heartbeat):[A-Za-z0-9_.\-]+)\b")


def poll_raven_runtime_logs(
    store: StateStore,
    emitter: PhoenixEmitter,
    cutoff_ts: float,
) -> tuple[int, int]:
    """Tail Raven gateway.log and subagent acp.log for operational errors."""
    spans_count = 0
    errors_count = 0
    log_files = [_ROOT / "logs" / "gateway.log"]
    for p in glob.glob(str(_ROOT / "subagent_sessions" / "*" / "acp" / "logs" / "acp.log")):
        log_files.append(Path(p))
    for p in glob.glob(str(_ROOT / "workspace" / "subagent_sessions" / "*" / "logs" / "acp.log")):
        log_files.append(Path(p))

    for lf in log_files:
        if not lf.exists():
            continue
        try:
            st = os.stat(lf)
        except OSError:
            continue
        if st.st_mtime < cutoff_ts:
            continue
        offset, prev_mtime_ns, prev_ctime = store.get_file_watermark(lf)
        if st.st_size < offset or (prev_ctime and int(st.st_ctime) != prev_ctime):
            offset = 0
        if st.st_size == offset and st.st_mtime_ns == prev_mtime_ns:
            continue

        try:
            with open(lf, "rb") as bf:
                bf.seek(offset)
                raw = bf.read()
                new_offset = bf.tell()
        except OSError:
            continue

        is_acp_log = lf.name == "acp.log"
        subagent_name = lf.parts[-4] if is_acp_log and len(lf.parts) >= 4 else "raven-gateway"
        active_acp_session: Optional[str] = None

        for idx, line in enumerate(raw.decode("utf-8", errors="replace").splitlines()):
            m = _RAVEN_LOG_RE.match(line)
            if not m:
                continue
            ts_str = m.group("ts")
            level = m.group("level")
            loc = m.group("loc")
            msg = m.group("msg")

            if "session acp:" in msg and "created at" in msg:
                sm = _SESSION_KEY_RE.search(msg)
                if sm:
                    active_acp_session = sm.group(1)

            dt = ensure_utc(ts_str)
            if dt.timestamp() < cutoff_ts:
                continue

            err_class: Optional[str] = None
            severity = level
            if "Error calling LLM (first_byte_timeout)" in msg and "_line:" in loc:
                err_class = "first_byte_timeout"
                severity = "ERROR"
            elif "Turn failed on " in msg and "first_byte_timeout" in msg:
                err_class = "first_byte_timeout_turn_failure"
                severity = "ERROR"
            elif "LLM error [first_byte_timeout] outlasted" in msg:
                err_class = "first_byte_timeout_retry"
            elif "MidStreamFallbackError" in msg:
                if "Turn failed on " in msg or "LLM returned error:" in msg:
                    err_class = "litellm_midstream_turn_failure"
                    severity = "ERROR"
                else:
                    err_class = "litellm_midstream_retry_warning"
            elif "session/prompt timed out after 600s" in msg:
                err_class = "acp_prompt_timeout_600s"
                severity = "ERROR"
            elif "stopReason='max_turn_requests'" in msg or "stopReason=max_turn_requests" in msg:
                err_class = "acp_max_turn_requests"
                severity = "WARNING"
            elif "MCP server 'trail': failed to connect" in msg or ("mcp_trail" in msg and "timed out" in msg):
                err_class = "mcp_trail_error"
                severity = "ERROR"
            elif "dropping message to " in msg and ("heartbeat" in msg or "cron" in msg):
                err_class = "scheduled_delivery_outlet_dropped"
                severity = "WARNING"
            elif "no outlet for channel 'cli'; dropping Text" in msg:
                err_class = "scheduled_delivery_outlet_dropped"
                severity = "WARNING"

            if not err_class:
                continue

            sk_match = _SESSION_KEY_RE.search(msg)
            if sk_match:
                sess_key = sk_match.group(1)
            elif err_class == "scheduled_delivery_outlet_dropped":
                sess_key = "heartbeat"
            else:
                sess_key = active_acp_session or f"raven:{subagent_name}"

            link = store.resolve_session_link("raven-acp", sess_key, int(dt.timestamp() * 1000)) if sess_key.startswith("acp:") else None
            if link:
                trace_id = link.trace_id
                parent_id = link.raven_node_span_id
                effective_sess = link.raven_session_key
            else:
                trace_id = make_trace_id("raven", sess_key)
                parent_id = make_span_id("raven", "root", sess_key)
                effective_sess = sess_key

            span_id = make_span_id("raven_log", str(lf), offset + idx, ts_str)
            emitter.add(
                build_span(
                    trace_id=trace_id,
                    span_id=span_id,
                    name=f"raven.error.{err_class}",
                    span_kind="CHAIN",
                    parent_id=parent_id,
                    start_time=dt,
                    end_time=dt,
                    status_code="ERROR",
                    status_message=msg[:1000],
                    attributes={
                        "session.id": effective_sess,
                        "child.session_id": sess_key if link else "",
                        "harness": "raven" if not is_acp_log else "raven-acp",
                        "agent.name": subagent_name,
                        "error.class": err_class,
                        "log.level": severity,
                        "log.location": loc,
                        "log.file": str(lf),
                        "output.value": msg,
                    },
                )
            )
            spans_count += 1
            errors_count += 1

        store.set_file_watermark("raven_log", lf, new_offset, st.st_mtime_ns, int(st.st_ctime))

    return spans_count, errors_count


class RavenAdapter:
    name = "raven"

    def discover(self, ctx: Context) -> bool:
        return self._root(ctx).is_dir()

    @staticmethod
    def _root(ctx: Context) -> Path:
        return ctx.dir_for("raven", paths.raven_root())

    def poll(self, ctx: Context, state: StateStore, store: PhoenixEmitter, cutoff_ts: float) -> PollResult:
        global _ROOT
        _ROOT = self._root(ctx)
        out: PollResult = {}
        try:
            out["raven_nodes"] = poll_raven_nodes(state, store, cutoff_ts)
        except Exception as e:
            state.record_source_health("raven_nodes", last_error=str(e))
            out["raven_nodes"] = (0, 0)
        try:
            out["raven_audit"] = poll_raven_audit_spans(state, store, cutoff_ts)
        except Exception as e:
            state.record_source_health("raven_audit", last_error=str(e))
            out["raven_audit"] = (0, 0)
        try:
            out["raven_sessions"] = poll_raven_sessions_and_subagents(state, store, cutoff_ts)
        except Exception as e:
            state.record_source_health("raven_sessions", last_error=str(e))
            out["raven_sessions"] = (0, 0)
        try:
            out["raven_logs"] = poll_raven_runtime_logs(state, store, cutoff_ts)
        except Exception as e:
            state.record_source_health("raven_logs", last_error=str(e))
            out["raven_logs"] = (0, 0)
        return out
