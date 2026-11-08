"""Correlated session/error digest and span pruning."""
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .core.paths import default_spans_db, default_state_db
from .platform.scheduler import collect_scheduled_tasks_and_crons


def prune_spans(phoenix_db: Path | None = None, retention_days: int = 90) -> dict[str, int]:
    phoenix_db = Path(phoenix_db) if phoenix_db else default_spans_db()
    if not phoenix_db.exists():
        return {"deleted_traces": 0, "deleted_spans": 0}
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(str(phoenix_db), timeout=30.0)
    try:
        cur_sp = con.execute("DELETE FROM spans WHERE start_time < ?", (cutoff_iso,))
        deleted_spans = cur_sp.rowcount
        cur = con.execute("DELETE FROM traces WHERE start_time < ?", (cutoff_iso,))
        deleted = cur.rowcount
        con.execute("DELETE FROM project_sessions WHERE start_time < ?", (cutoff_iso,))
        con.commit()
        return {"deleted_traces": deleted, "deleted_spans": deleted_spans}
    finally:
        con.close()


def generate_digest(
    hours: float = 24.0,
    phoenix_db: Path | None = None,
    state_db: Path | None = None,
    as_json: bool = False,
) -> str:
    phoenix_db = Path(phoenix_db) if phoenix_db else default_spans_db()
    state_db = Path(state_db) if state_db else default_state_db()
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_iso = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
    cutoff_epoch = cutoff_dt.timestamp()

    con = sqlite3.connect(f"file:{phoenix_db.as_posix()}?mode=ro", uri=True, timeout=10.0)
    con.row_factory = sqlite3.Row

    # 1. Per-harness span, trace, token, tool, skill, error summary from spans
    rows = con.execute(
        """
        SELECT
            COALESCE(
                JSON_EXTRACT(s.attributes, '$.harness'),
                'unknown'
            ) AS harness,
            s.span_kind,
            s.status_code,
            s.name,
            s.trace_rowid,
            s.parent_id,
            COALESCE(s.llm_token_count_prompt, JSON_EXTRACT(s.attributes, '$.llm.token_count.prompt'), 0) AS prompt_tok,
            COALESCE(s.llm_token_count_completion, JSON_EXTRACT(s.attributes, '$.llm.token_count.completion'), 0) AS comp_tok,
            (JULIANDAY(s.end_time) - JULIANDAY(s.start_time)) * 86400000.0 AS dur_ms,
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
        WHERE s.start_time >= ?
        """,
        (cutoff_iso,),
    ).fetchall()
    con.close()

    stats: dict[str, dict[str, Any]] = defaultdict(
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
            "latencies_ms": [],
        }
    )
    error_classes: dict[tuple[str, str], int] = defaultdict(int)
    tool_counts: dict[tuple[str, str], int] = defaultdict(int)
    tool_errors: dict[tuple[str, str], int] = defaultdict(int)
    skill_loaded_counts: dict[tuple[str, str], int] = defaultdict(int)
    skill_injected_counts: dict[tuple[str, str], int] = defaultdict(int)

    for r in rows:
        h = str(r["harness"] or "unknown")
        st = stats[h]
        st["spans"] += 1
        st["traces"].add(r["trace_rowid"])
        sid = r["child_session_id"] or r["session_id"]
        if sid:
            st["sessions"].add(str(sid))

        sk = str(r["span_kind"] or "")
        nm = str(r["name"] or "")
        if ".turn" in nm or "session.turn" in nm or "codex.turn" in nm:
            st["turns"] += 1
        if sk == "LLM":
            st["llm_spans"] += 1
        if sk == "TOOL":
            st["tool_calls"] += 1
            tname = str(r["tool_name"] or nm.split(".")[-1] or "tool").split(" ")[0]
            tool_counts[(h, tname)] += 1
            if r["status_code"] == "ERROR":
                tool_errors[(h, tname)] += 1
        if r["is_skill"] in (1, True, "true", "True") or "skill." in nm or ".skill." in nm:
            st["skill_calls"] += 1
            sn_raw = r["skill_names"]
            if nm == "skill.inject" and sn_raw:
                try:
                    parsed_sn = json.loads(sn_raw) if isinstance(sn_raw, str) and sn_raw.startswith("[") else [str(sn_raw)]
                    for s_item in parsed_sn:
                        if s_item:
                            skill_injected_counts[(h, str(s_item))] += 1
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
                skill_loaded_counts[(h, str(extracted_skill or r["tool_name"] or nm))] += 1

        st["prompt_tokens"] += int(r["prompt_tok"] or 0)
        st["completion_tokens"] += int(r["comp_tok"] or 0)

        dur = float(r["dur_ms"] or 0.0)
        if dur > 1.0 and sk in ("LLM", "AGENT", "TOOL"):
            st["latencies_ms"].append(dur)

        if r["status_code"] == "ERROR" or r["err_class"]:
            st["error_spans"] += 1
            ec = str(r["err_class"] or "unspecified_error")
            cnt = int(r["err_count"] or 1)
            st["error_events"] += cnt
            error_classes[(h, ec)] += cnt

    # 2. Correlation links from agent_tap_state.sqlite
    scon = sqlite3.connect(f"file:{state_db.as_posix()}?mode=ro", uri=True, timeout=5.0)
    scon.row_factory = sqlite3.Row
    link_rows = scon.execute(
        """
        SELECT child_harness, confidence, COUNT(*) AS cnt, COUNT(DISTINCT child_session_id) AS distinct_sessions
        FROM session_links
        WHERE COALESCE(started_at_ms, 0) >= ?
        GROUP BY child_harness, confidence
        ORDER BY cnt DESC
        """,
        (int(cutoff_epoch * 1000),),
    ).fetchall()
    health_rows = scon.execute("SELECT * FROM source_health ORDER BY source").fetchall()
    scon.close()

    cron_data = collect_scheduled_tasks_and_crons(cutoff_epoch, phoenix_db)

    if as_json:
        serializable_stats = {}
        for h, d in stats.items():
            lats = sorted(d["latencies_ms"])
            p50 = round(lats[len(lats) // 2], 1) if lats else 0.0
            p90 = round(lats[int(len(lats) * 0.9)], 1) if lats else 0.0
            serializable_stats[h] = {
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
                "error_spans": d["error_spans"],
                "error_events": d["error_events"],
                "p50_ms": p50,
                "p90_ms": p90,
            }
        return json.dumps(
            {
                "window_hours": hours,
                "harnesses": serializable_stats,
                "error_classes": [
                    {"harness": h, "error_class": ec, "count": cnt}
                    for (h, ec), cnt in sorted(error_classes.items(), key=lambda x: x[1], reverse=True)
                ],
                "session_links": [dict(r) for r in link_rows],
                "scheduled_tasks": cron_data,
                "source_health": [dict(r) for r in health_rows],
            },
            indent=2,
        )

    lines = [
        f"# agent-tap Digest (Last {hours:g} Hours - Since {cutoff_iso} UTC)",
        "",
        "## 1. Per-Harness Activity & Telemetry Summary (Local SQLite)",
        "| Harness | Sessions | Traces | Total Spans | Turns | Tool Calls | Skill Spans | Total Tokens (In + Out) | Error Spans (Events) | Latency p50 / p90 (ms) |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    for h in sorted(stats.keys()):
        d = stats[h]
        lats = sorted(d["latencies_ms"])
        p50 = f"{lats[len(lats) // 2]:.0f}" if lats else "-"
        p90 = f"{lats[int(len(lats) * 0.9)]:.0f}" if lats else "-"
        tot_tok = d["prompt_tokens"] + d["completion_tokens"]
        lines.append(
            f"| **{h}** | {len(d['sessions'])} | {len(d['traces'])} | {d['spans']:,} | {d['turns']:,} | "
            f"{d['tool_calls']:,} | {d['skill_calls']:,} | {tot_tok:,} ({d['prompt_tokens']:,} + {d['completion_tokens']:,}) | "
            f"{d['error_spans']:,} ({d['error_events']:,}) | {p50} / {p90} |"
        )

    lines.extend(
        [
            "",
            "## 2. Top 20 Most Used Tools (Across All Harnesses)",
            "| Rank | Harness | Tool Name | Calls | Errors |",
            "| :---: | :--- | :--- | :---: | :---: |",
        ]
    )
    for idx, ((h, tname), cnt) in enumerate(sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:20], 1):
        err_c = tool_errors.get((h, tname), 0)
        lines.append(f"| {idx} | **{h}** | `{tname}` | {cnt:,} | {err_c:,} |")

    lines.extend(
        [
            "",
            "## 3. Most Used / Loaded Skills (Explicit Tool Invocations + Preloaded Startup Injections)",
            "| Category | Harness | Skill / Operation | Count |",
            "| :--- | :--- | :--- | :---: |",
        ]
    )
    for (h, sname), cnt in sorted(skill_loaded_counts.items(), key=lambda x: x[1], reverse=True)[:20]:
        lines.append(f"| **On-Demand / Tool** | **{h}** | `{sname}` | {cnt:,} |")
    for (h, sname), cnt in sorted(skill_injected_counts.items(), key=lambda x: x[1], reverse=True)[:15]:
        lines.append(f"| **Startup Injected** | **{h}** | `{sname}` | {cnt:,} |")

    lines.extend(
        [
            "",
            "## 4. Normalized Runtime & Session Error Classes",
            "| Harness | Error Class | Event Count |",
            "| :--- | :--- | :---: |",
        ]
    )
    for (h, ec), cnt in sorted(error_classes.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"| **{h}** | `{ec}` | {cnt:,} |")

    lines.extend(
        [
            "",
            "## 5. Parent-to-Child Correlation Links (`Raven -> ACP -> Child`)",
            "| Child Harness | Confidence | Linked Node Runs | Distinct Child Sessions |",
            "| :--- | :---: | :---: | :---: |",
        ]
    )
    for lr in link_rows:
        lines.append(
            f"| **{lr['child_harness']}** | `{lr['confidence']}` | {lr['cnt']} | {lr['distinct_sessions']} |"
        )

    lines.extend(
        [
            "",
            "## 6. Scheduled Tasks & Crons (OS scheduler + Raven + Hermes crons)",
            "| Source | Name | Profile | Schedule | State | Last Run (UTC) | Next Run (UTC) | Last Status | Exit Code |",
            "| :--- | :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: |",
        ]
    )
    for sc in cron_data.get("configured_schedules", []):
        lines.append(
            f"| **{sc['source']}** | `{sc['name']}` | `{sc['profile']}` | `{sc['schedule']}` | "
            f"{sc['state']} | {sc['last_run']} | {sc['next_run']} | `{sc['last_status']}` | {sc['exit_code']} |"
        )

    return "\n".join(lines)

