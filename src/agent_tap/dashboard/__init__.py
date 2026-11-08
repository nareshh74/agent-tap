import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from ..core.config import Config, load_config
from ..core.schema import service_kind_for
from ..platform.scheduler import collect_scheduled_tasks_and_crons


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(max(int(len(sorted_vals) * q), 0), len(sorted_vals) - 1)
    return round(sorted_vals[idx], 1)


def compute_dashboard_payload(
    hours: float = 24.0,
    harness_filter: Optional[str] = None,
    spans_db: Path | None = None,
    state_db: Path | None = None,
    config: Config | None = None,
) -> dict[str, Any]:
    config = config or load_config()
    phoenix_db = Path(spans_db) if spans_db else config.spans_db
    state_db = Path(state_db) if state_db else config.state_db
    now_dt = datetime.now(timezone.utc)
    cutoff_dt = now_dt - timedelta(hours=hours)
    cutoff_iso = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
    cutoff_epoch_ms = int(cutoff_dt.timestamp() * 1000)

    con = sqlite3.connect(f"file:{phoenix_db.as_posix()}?mode=ro", uri=True, timeout=15.0)
    con.row_factory = sqlite3.Row

    sql = """
        SELECT
            s.id AS span_rowid,
            s.span_id,
            s.parent_id,
            s.trace_rowid,
            t.trace_id,
            s.span_kind,
            s.status_code,
            s.status_message,
            s.name,
            s.start_time,
            s.end_time,
            COALESCE(JSON_EXTRACT(s.attributes, '$.harness'), 'unknown') AS harness,
            JSON_EXTRACT(s.attributes, '$.hermes.profile') AS hermes_profile,
            JSON_EXTRACT(s.attributes, '$.service.kind') AS svc_kind,
            JSON_EXTRACT(s.attributes, '$.service.status') AS svc_status,
            JSON_EXTRACT(s.attributes, '$.service.endpoint') AS svc_endpoint,
            JSON_EXTRACT(s.attributes, '$.service.latency_ms') AS svc_latency,
            JSON_EXTRACT(s.attributes, '$.http.status_code') AS http_status,
            COALESCE(s.llm_token_count_prompt, JSON_EXTRACT(s.attributes, '$.llm.token_count.prompt'), 0) AS prompt_tok,
            COALESCE(s.llm_token_count_completion, JSON_EXTRACT(s.attributes, '$.llm.token_count.completion'), 0) AS comp_tok,
            COALESCE(JSON_EXTRACT(s.attributes, '$.hermes.estimated_cost_usd'), 0.0) AS cost_usd,
            (JULIANDAY(s.end_time) - JULIANDAY(s.start_time)) * 86400000.0 AS dur_ms,
            JSON_EXTRACT(s.attributes, '$.omniroute.ttft_ms') AS ttft_ms,
            JSON_EXTRACT(s.attributes, '$.error.class') AS err_class,
            JSON_EXTRACT(s.attributes, '$.error.count') AS err_count,
            JSON_EXTRACT(s.attributes, '$.session.id') AS session_id,
            JSON_EXTRACT(s.attributes, '$.child.session_id') AS child_session_id,
            JSON_EXTRACT(s.attributes, '$.is_skill') AS is_skill,
            JSON_EXTRACT(s.attributes, '$.tool.name') AS tool_name,
            JSON_EXTRACT(s.attributes, '$.tool.parameters') AS tool_params,
            JSON_EXTRACT(s.attributes, '$.skill.names') AS skill_names,
            JSON_EXTRACT(s.attributes, '$.input.value') AS input_val,
            JSON_EXTRACT(s.attributes, '$.output.value') AS output_val
        FROM spans s
        JOIN traces t ON t.id = s.trace_rowid
        WHERE s.start_time >= ?
    """
    params: list[Any] = [cutoff_iso]
    if harness_filter and harness_filter != "all":
        sql += " AND COALESCE(JSON_EXTRACT(s.attributes, '$.harness'), 'unknown') = ?"
        params.append(harness_filter)

    rows = con.execute(sql, tuple(params)).fetchall()
    # Names present in the window, regardless of the active filter, so the dropdown keeps every choice.
    seen = con.execute(
        "SELECT COALESCE(JSON_EXTRACT(attributes, '$.harness'), 'unknown') AS h, "
        "MAX(JSON_EXTRACT(attributes, '$.service.kind')) AS k FROM spans WHERE start_time >= ? GROUP BY h",
        (cutoff_iso,),
    ).fetchall()
    con.close()
    cfg_kinds = config.service_kinds()
    kinds: dict[str, str] = {}
    available: dict[str, dict[str, Any]] = {}
    for sr in seen:
        nm_ = str(sr["h"])
        kd_ = sr["k"] or service_kind_for(nm_, cfg_kinds)
        kinds[nm_] = kd_
        available[nm_] = {"name": nm_, "kind": kd_}
    for sv in config.services:
        if sv.get("name"):
            available.setdefault(str(sv["name"]), {"name": str(sv["name"]), "kind": str(sv.get("kind", "service"))})

    # Global KPIs
    all_sessions: set[str] = set()
    all_traces: set[str] = set()
    all_latencies: list[float] = []
    all_ttfts: list[float] = []
    total_spans = 0
    total_turns = 0
    total_llm_spans = 0
    total_tool_calls = 0
    total_skill_calls = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_error_spans = 0
    total_error_events = 0
    total_cost_usd = 0.0

    # Per-harness stats
    harness_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "spans": 0,
            "sessions": set(),
            "traces": set(),
            "turns": 0,
            "llm_spans": 0,
            "tool_calls": 0,
            "skill_calls": 0,
            "error_spans": 0,
            "error_events": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
            "latencies_ms": [],
        }
    )
    # Per-service stats (non-harness kinds)
    service_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "checks": 0,
            "requests": 0,
            "errors": 0,
            "probe_lat": [],
            "req_lat": [],
            "status": "unknown",
            "status_at": "",
            "endpoint": "",
            "last_seen": "",
            "trace_id": "",
            "error_trace_id": "",
            "err_at": "",
        }
    )

    # Tool leaderboard
    tool_map: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "calls": 0,
            "errors": 0,
            "latencies_ms": [],
            "last_seen": "",
            "sample_trace_id": "",
        }
    )

    # Skill leaderboard
    skill_ondemand_map: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "errors": 0, "last_seen": "", "sample_trace_id": ""}
    )
    skill_injected_map: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "last_seen": "", "sample_trace_id": ""}
    )

    # Error class leaderboard
    error_map: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "spans": 0,
            "events": 0,
            "last_seen": "",
            "sample_msg": "",
            "sample_trace_id": "",
            "sample_span_id": "",
        }
    )

    # Per-child-session rollup for Parent-to-Child Correlation Explorer
    child_session_rollup: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "spans": 0,
            "tool_calls": 0,
            "errors": 0,
            "tokens": 0,
            "trace_id": "",
        }
    )

    for r in rows:
        h = str(r["harness"] or "unknown")
        prof = str(r["hermes_profile"] or ("default" if h == "hermes" else ""))
        trace_id = str(r["trace_id"] or "")
        span_id = str(r["span_id"] or "")
        st_time = str(r["start_time"] or "")
        sk = str(r["span_kind"] or "")
        nm = str(r["name"] or "")
        sid = str(r["child_session_id"] or r["session_id"] or "")

        total_spans += 1
        all_traces.add(trace_id)
        if sid:
            all_sessions.add(f"{h}:{sid}")

        if r["svc_kind"]:
            kinds[h] = str(r["svc_kind"])
        else:
            kinds.setdefault(h, service_kind_for(h, cfg_kinds))
        hs = harness_stats[h]
        hs["spans"] += 1
        hs["traces"].add(trace_id)
        if sid:
            hs["sessions"].add(sid)

        if r["child_session_id"]:
            cr = child_session_rollup[(h, str(r["child_session_id"]))]
            cr["spans"] += 1
            if not cr["trace_id"]:
                cr["trace_id"] = trace_id

        is_turn = ".turn" in nm or "session.turn" in nm or "codex.turn" in nm
        if is_turn:
            total_turns += 1
            hs["turns"] += 1
        if sk == "LLM":
            total_llm_spans += 1
            hs["llm_spans"] += 1

        ptok = int(r["prompt_tok"] or 0)
        ctok = int(r["comp_tok"] or 0)
        cost = float(r["cost_usd"] or 0.0)
        total_prompt_tokens += ptok
        total_completion_tokens += ctok
        total_cost_usd += cost
        hs["prompt_tokens"] += ptok
        hs["completion_tokens"] += ctok
        hs["cost_usd"] += cost

        if r["child_session_id"]:
            child_session_rollup[(h, str(r["child_session_id"]))]["tokens"] += ptok + ctok

        dur = float(r["dur_ms"] or 0.0)
        if dur > 1.0 and sk in ("LLM", "AGENT", "TOOL"):
            all_latencies.append(dur)
            hs["latencies_ms"].append(dur)

        if r["ttft_ms"] is not None:
            try:
                tval = float(r["ttft_ms"])
                if tval > 0:
                    all_ttfts.append(tval)
            except Exception:
                pass

        is_err = r["status_code"] == "ERROR" or bool(r["err_class"])

        if kinds[h] != "harness":
            sv = service_stats[h]
            is_probe = sk == "SERVICE" and bool(r["svc_status"])
            if is_probe:
                sv["checks"] += 1
                if r["svc_latency"] is not None:
                    try:
                        sv["probe_lat"].append(float(r["svc_latency"]))
                    except Exception:
                        pass
                elif dur > 0:
                    sv["probe_lat"].append(dur)
                if st_time >= sv["status_at"]:
                    sv["status_at"] = st_time
                    sv["status"] = str(r["svc_status"])
                    if r["svc_endpoint"]:
                        sv["endpoint"] = str(r["svc_endpoint"])
            else:
                sv["requests"] += 1
                if dur > 0:
                    sv["req_lat"].append(dur)
                if r["svc_endpoint"] and not sv["endpoint"]:
                    sv["endpoint"] = str(r["svc_endpoint"])
            if is_err:
                sv["errors"] += 1
            if st_time >= sv["last_seen"]:
                sv["last_seen"] = st_time
                sv["trace_id"] = trace_id
            if is_err and st_time >= sv["err_at"]:
                sv["err_at"] = st_time
                sv["error_trace_id"] = trace_id

        if sk == "TOOL":
            total_tool_calls += 1
            hs["tool_calls"] += 1
            if r["child_session_id"]:
                child_session_rollup[(h, str(r["child_session_id"]))]["tool_calls"] += 1
            tname = str(r["tool_name"] or nm.split(".")[-1] or "tool").split(" ")[0]
            tm = tool_map[(h, tname)]
            tm["calls"] += 1
            if is_err:
                tm["errors"] += 1
            if dur > 0:
                tm["latencies_ms"].append(dur)
            if st_time >= tm["last_seen"]:
                tm["last_seen"] = st_time
                tm["sample_trace_id"] = trace_id

        is_skill_span = r["is_skill"] in (1, True, "true", "True") or "skill." in nm or ".skill." in nm
        if is_skill_span:
            total_skill_calls += 1
            hs["skill_calls"] += 1
            sn_raw = r["skill_names"]
            if nm == "skill.inject" and sn_raw:
                try:
                    parsed_sn = json.loads(sn_raw) if isinstance(sn_raw, str) and sn_raw.startswith("[") else [str(sn_raw)]
                    for s_item in parsed_sn:
                        if s_item:
                            sm = skill_injected_map[(h, str(s_item))]
                            sm["count"] += 1
                            if st_time >= sm["last_seen"]:
                                sm["last_seen"] = st_time
                                sm["sample_trace_id"] = trace_id
                except Exception:
                    pass
            else:
                extracted_skill = None
                if sn_raw and sn_raw not in ("[]", ""):
                    extracted_skill = str(sn_raw).strip('[]"')
                if not extracted_skill and r["tool_params"]:
                    try:
                        tp = json.loads(r["tool_params"]) if isinstance(r["tool_params"], str) else r["tool_params"]
                        if isinstance(tp, dict):
                            extracted_skill = tp.get("skill_id") or tp.get("name") or tp.get("skill")
                    except Exception:
                        pass
                if not extracted_skill and r["input_val"]:
                    try:
                        iv = json.loads(r["input_val"])
                        if isinstance(iv, dict):
                            extracted_skill = iv.get("skill_id") or iv.get("name") or iv.get("skill") or iv.get("query")
                    except Exception:
                        pass
                if not extracted_skill and r["output_val"] and '"name":' in str(r["output_val"]):
                    try:
                        ov = json.loads(r["output_val"])
                        if isinstance(ov, dict) and ov.get("name"):
                            extracted_skill = ov.get("name")
                    except Exception:
                        pass
                sname = str(extracted_skill or r["tool_name"] or nm)
                som = skill_ondemand_map[(h, sname)]
                som["count"] += 1
                if is_err:
                    som["errors"] += 1
                if st_time >= som["last_seen"]:
                    som["last_seen"] = st_time
                    som["sample_trace_id"] = trace_id

        if is_err:
            ec = str(r["err_class"] or "unspecified_error")
            cnt = int(r["err_count"] or 1)
            total_error_spans += 1
            total_error_events += cnt
            hs["error_spans"] += 1
            hs["error_events"] += cnt
            if r["child_session_id"]:
                child_session_rollup[(h, str(r["child_session_id"]))]["errors"] += cnt

            em = error_map[(h, prof or "-", ec)]
            em["spans"] += 1
            em["events"] += cnt
            if st_time >= em["last_seen"]:
                em["last_seen"] = st_time
                em["sample_trace_id"] = trace_id
                em["sample_span_id"] = span_id
                msg_sample = str(r["status_message"] or r["output_val"] or "")
                em["sample_msg"] = msg_sample[:240]


    # Sort latencies
    all_latencies.sort()
    all_ttfts.sort()

    # Harness activity (kind == harness only)
    harnesses_table = []
    for h in sorted(k for k in harness_stats if kinds.get(k) == "harness"):
        d = harness_stats[h]
        lats = sorted(d["latencies_ms"])
        harnesses_table.append(
            {
                "harness": h,
                "sessions": len(d["sessions"]),
                "traces": len(d["traces"]),
                "spans": d["spans"],
                "turns": d["turns"],
                "llm_spans": d["llm_spans"],
                "tool_calls": d["tool_calls"],
                "skill_calls": d["skill_calls"],
                "prompt_tokens": d["prompt_tokens"],
                "completion_tokens": d["completion_tokens"],
                "total_tokens": d["prompt_tokens"] + d["completion_tokens"],
                "cost_usd": round(d["cost_usd"], 4),
                "error_spans": d["error_spans"],
                "error_events": d["error_events"],
                "p50_ms": _percentile(lats, 0.50),
                "p90_ms": _percentile(lats, 0.90),
                "phoenix_filter": f"harness == '{h}'",
            }
        )

    # Running services and gateways (kind != harness)
    cfg_by_name = {str(x.get("name")): x for x in config.services if x.get("name")}
    names = {k for k, v in kinds.items() if v != "harness"} & set(service_stats)
    names |= {n for n, v in available.items() if v["kind"] != "harness" and harness_filter in (None, "all", n)}
    services_table = []
    for name in sorted(names):
        d = service_stats[name]
        lats = sorted(d["req_lat"] or d["probe_lat"])
        cfgsvc = cfg_by_name.get(name, {})
        endpoint = d["endpoint"] or str(cfgsvc.get("health_url") or cfgsvc.get("docker_container") or "-")
        services_table.append(
            {
                "service": name,
                "kind": kinds.get(name) or available.get(name, {}).get("kind", "service"),
                "status": d["status"],
                "endpoint": endpoint,
                "checks": d["checks"],
                "requests": d["requests"],
                "errors": d["errors"],
                "p50_ms": _percentile(lats, 0.50),
                "p90_ms": _percentile(lats, 0.90),
                "last_seen": d["last_seen"],
                "trace_id": d["error_trace_id"] or d["trace_id"],
                "phoenix_filter": f"harness == '{name}'",
            }
        )
    service_summary = {"total": len(services_table)}
    for st_name in ("healthy", "degraded", "down", "unknown"):
        service_summary[st_name] = sum(1 for x in services_table if x["status"] == st_name)

    # Tool usage
    tools_table = []
    for (h, tname), d in sorted(tool_map.items(), key=lambda x: x[1]["calls"], reverse=True)[:50]:
        lats = sorted(d["latencies_ms"])
        calls = d["calls"]
        errs = d["errors"]
        tools_table.append(
            {
                "harness": h,
                "tool_name": tname,
                "calls": calls,
                "errors": errs,
                "error_rate_pct": round((errs * 100.0 / calls), 1) if calls else 0.0,
                "p50_ms": _percentile(lats, 0.50),
                "p90_ms": _percentile(lats, 0.90),
                "last_seen": d["last_seen"],
                "sample_trace_id": d["sample_trace_id"],
                "trace_id": d["sample_trace_id"] or "",
                "phoenix_filter": f"span_kind == 'TOOL' and harness == '{h}' and tool.name == '{tname}'",
            }
        )

    # Skill invocations
    skills_ondemand_table = []
    for (h, sname), d in sorted(skill_ondemand_map.items(), key=lambda x: x[1]["count"], reverse=True)[:40]:
        skills_ondemand_table.append(
            {
                "category": "On-Demand / Tool",
                "harness": h,
                "skill_name": sname,
                "count": d["count"],
                "errors": d["errors"],
                "last_seen": d["last_seen"],
                "sample_trace_id": d["sample_trace_id"],
                "trace_id": d["sample_trace_id"] or "",
                "phoenix_filter": f"harness == '{h}' and 'skill' in name and name != 'skill.inject'",
            }
        )

    skills_injected_table = []
    for (h, sname), d in sorted(skill_injected_map.items(), key=lambda x: x[1]["count"], reverse=True)[:30]:
        skills_injected_table.append(
            {
                "category": "Startup Injected",
                "harness": h,
                "skill_name": sname,
                "count": d["count"],
                "last_seen": d["last_seen"],
                "sample_trace_id": d["sample_trace_id"],
                "trace_id": d["sample_trace_id"] or "",
                "phoenix_filter": f"name == 'skill.inject' and '{sname}' in skill.names",
            }
        )

    # Crashes, timeouts and operational errors
    errors_table = []
    for (h, prof, ec), d in sorted(error_map.items(), key=lambda x: x[1]["events"], reverse=True):
        errors_table.append(
            {
                "harness": h,
                "profile": prof,
                "error_class": ec,
                "events": d["events"],
                "spans": d["spans"],
                "last_seen": d["last_seen"],
                "sample_msg": d["sample_msg"],
                "sample_trace_id": d["sample_trace_id"],
                "trace_id": d["sample_trace_id"] or "",
                "phoenix_filter": f"harness == '{h}' and error.class == '{ec}'",
            }
        )

    # Parent-to-child correlation
    scon = sqlite3.connect(f"file:{state_db.as_posix()}?mode=ro", uri=True, timeout=5.0)
    scon.row_factory = sqlite3.Row
    link_summary_rows = scon.execute(
        """
        SELECT child_harness, confidence, COUNT(*) AS cnt, COUNT(DISTINCT child_session_id) AS distinct_sessions
        FROM session_links
        WHERE COALESCE(started_at_ms, 0) >= ?
        GROUP BY child_harness, confidence
        ORDER BY cnt DESC
        """,
        (cutoff_epoch_ms,),
    ).fetchall()

    detailed_links_sql = """
        SELECT
            child_harness,
            child_session_id,
            raven_session_key,
            raven_call_id,
            raven_node_span_id,
            trace_id,
            started_at_ms,
            ended_at_ms,
            confidence
        FROM session_links
        WHERE COALESCE(started_at_ms, 0) >= ?
    """
    dl_params: list[Any] = [cutoff_epoch_ms]
    if harness_filter and harness_filter not in ("all", "raven", "omniroute"):
        detailed_links_sql += " AND child_harness = ?"
        dl_params.append(harness_filter)
    detailed_links_sql += " ORDER BY COALESCE(started_at_ms, 0) DESC LIMIT 100"
    detailed_link_rows = scon.execute(detailed_links_sql, tuple(dl_params)).fetchall()
    health_rows = scon.execute("SELECT * FROM source_health ORDER BY source").fetchall()
    scon.close()

    correlation_runs = []
    for lr in detailed_link_rows:
        ch = str(lr["child_harness"])
        csid = str(lr["child_session_id"])
        st_ms = lr["started_at_ms"]
        en_ms = lr["ended_at_ms"]
        dur_s = round((en_ms - st_ms) / 1000.0, 1) if (st_ms and en_ms and en_ms >= st_ms) else None
        st_iso = datetime.fromtimestamp(st_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if st_ms else "-"
        rollup = child_session_rollup.get((ch, csid), {"spans": 0, "tool_calls": 0, "errors": 0, "tokens": 0})
        tr_id = str(lr["trace_id"])
        correlation_runs.append(
            {
                "started_at": st_iso,
                "raven_session_key": lr["raven_session_key"],
                "raven_call_id": lr["raven_call_id"],
                "child_harness": ch,
                "child_session_id": csid,
                "confidence": lr["confidence"],
                "duration_s": dur_s,
                "child_spans": rollup["spans"],
                "child_tool_calls": rollup["tool_calls"],
                "child_tokens": rollup["tokens"],
                "child_errors": rollup["errors"],
                "trace_id": tr_id,
            }
        )

    crons_data = collect_scheduled_tasks_and_crons(cutoff_dt.timestamp(), phoenix_db, harness_filter)

    return {
        "generated_at": now_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "window_hours": hours,
        "harness_filter": harness_filter or "all",
        "summary": {
            "sessions": len(all_sessions),
            "traces": len(all_traces),
            "spans": total_spans,
            "turns": total_turns,
            "llm_spans": total_llm_spans,
            "tool_calls": total_tool_calls,
            "skill_calls": total_skill_calls,
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "total_tokens": total_prompt_tokens + total_completion_tokens,
            "cost_usd": round(total_cost_usd, 4),
            "error_spans": total_error_spans,
            "error_events": total_error_events,
            "latency_p50_ms": _percentile(all_latencies, 0.50),
            "latency_p90_ms": _percentile(all_latencies, 0.90),
            "ttft_p50_ms": _percentile(all_ttfts, 0.50),
            "ttft_p90_ms": _percentile(all_ttfts, 0.90),
            "correlated_runs": sum(int(r["cnt"]) for r in link_summary_rows),
        },
        "harnesses": harnesses_table,
        "services": services_table,
        "service_summary": service_summary,
        "available": {
            "harnesses": sorted(n for n, v in available.items() if v["kind"] == "harness"),
            "services": sorted((v for v in available.values() if v["kind"] != "harness"), key=lambda v: v["name"]),
        },
        "top_tools": tools_table,
        "skills_ondemand": skills_ondemand_table,
        "skills_injected": skills_injected_table,
        "error_classes": errors_table,
        "correlation_summary": [dict(r) for r in link_summary_rows],
        "correlation_runs": correlation_runs,
        "source_health": [dict(r) for r in health_rows],
        "crons": crons_data,
    }


def _index_html() -> str:
    return resources.files("agent_tap.dashboard").joinpath("static/index.html").read_text(encoding="utf-8")


def create_dashboard_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    app = FastAPI(title="agent-tap KPI Dashboard", version=__import__("agent_tap").__version__)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/digest")
    def api_digest(
        hours: float = Query(24.0, ge=0.1, le=2200.0),
        harness: str = Query("all"),
    ) -> JSONResponse:
        return JSONResponse(compute_dashboard_payload(hours=hours, harness_filter=harness, config=cfg))

    @app.get("/api/trace/{trace_id}")
    def api_trace(
        trace_id: str,
        error_class: Optional[str] = Query(None),
        tool_name: Optional[str] = Query(None),
        hours: float = Query(24.0, ge=0.1, le=2200.0),
        limit: int = Query(300, ge=1, le=1000),
    ) -> JSONResponse:
        where, params = [], []
        if trace_id and trace_id != "_":
            where.append("t.trace_id = ?")
            params.append(trace_id)
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            where.append("s.start_time >= ?")
            params.append(cutoff.strftime("%Y-%m-%d %H:%M:%S"))
        if error_class:
            where.append("JSON_EXTRACT(s.attributes, '$.error.class') = ?")
            params.append(error_class)
        if tool_name:
            where.append("JSON_EXTRACT(s.attributes, '$.tool.name') = ?")
            params.append(tool_name)
        con = sqlite3.connect(f"file:{cfg.spans_db.as_posix()}?mode=ro", uri=True, timeout=15.0)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT t.trace_id, s.span_id, s.name, s.span_kind, s.status_code, s.status_message, s.start_time, s.end_time, "
                "COALESCE(JSON_EXTRACT(s.attributes, '$.harness'), 'unknown') AS harness, "
                "(JULIANDAY(s.end_time) - JULIANDAY(s.start_time)) * 86400000.0 AS dur_ms, s.attributes "
                "FROM spans s JOIN traces t ON t.id = s.trace_rowid WHERE "
                + " AND ".join(where)
                + " ORDER BY s.start_time DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        finally:
            con.close()
        spans = [
            {
                "trace_id": r["trace_id"],
                "span_id": r["span_id"],
                "name": r["name"],
                "kind": r["span_kind"],
                "status_code": r["status_code"],
                "status_message": r["status_message"] or "",
                "start_time": str(r["start_time"])[:23],
                "duration_ms": r["dur_ms"],
                "harness": r["harness"],
                "attributes": str(r["attributes"] or "")[:4000],
            }
            for r in rows[:limit]
        ]
        return JSONResponse({"trace_id": trace_id, "spans": spans, "truncated": len(rows) > limit})

    @app.get("/api/bug-report")
    def api_bug_report(service: Optional[str] = Query(None), error_text: Optional[str] = Query(None)) -> JSONResponse:
        from ..bugreport import build_bundle, issue_url, render_markdown

        bundle = build_bundle(cfg, error_text=error_text, service=service)
        return JSONResponse({"markdown": render_markdown(bundle), "url": issue_url(bundle)})

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(_index_html())

    return app


def run_dashboard_server(host: str | None = None, port: int | None = None, config: Config | None = None) -> None:
    cfg = config or load_config()
    uvicorn.run(create_dashboard_app(cfg), host=host or cfg.host, port=port or cfg.port, log_level="warning")
