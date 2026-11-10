import csv
import glob
import io
import plistlib
import sys
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..core import paths
from ..emitter import ensure_utc
from ..state import make_trace_id

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)



def _fmt_utc_str(val: Any) -> str:
    if not val:
        return "-"
    try:
        dt = ensure_utc(val)
        if dt.year < 2005:
            return "-"
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(val)[:19]


DEFAULT_NAME_PATTERN = r"agent-?tap|hermes|raven|claude|codex|copilot|omniroute|agent"

_OS_CACHE: dict[str, Any] = {"ts": 0.0, "key": "", "rows": [], "refreshing": False}
_OS_CACHE_TTL = 300.0


def _harness_for(text: str) -> str:
    t = text.lower()
    if "hermes" in t:
        return "hermes"
    if "raven" in t:
        return "raven"
    if "agent-tap" in t or "agenttap" in t:
        return "agent-tap"
    return "os-schedule"


def _row(source: str, profile: str, ident: str, name: str, schedule: str, state: str, enabled: bool,
         last_run: str, next_run: str, last_status: str, exit_code: int, total_runs: int,
         command: str, last_error: str) -> dict[str, Any]:
    return {
        "source": source, "harness": _harness_for(f"{name} {command}"), "profile": profile, "id": ident,
        "name": name, "schedule": schedule, "state": state, "enabled": enabled, "last_run": last_run,
        "next_run": next_run, "last_status": last_status, "exit_code": exit_code, "total_runs": total_runs,
        "command_or_prompt": command, "last_error": last_error, "trace_id": "",
    }


def parse_schtasks_csv(text: str, name_pattern: str = DEFAULT_NAME_PATTERN) -> list[dict[str, Any]]:
    """Parse `schtasks /query /fo CSV /v` output, keeping tasks whose name/command matches name_pattern."""
    pat = re.compile(name_pattern, re.IGNORECASE)
    rows_out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in csv.DictReader(io.StringIO(text)):
        tname = (row.get("TaskName") or "").strip()
        if not tname or tname == "TaskName" or tname.startswith("\\Microsoft\\") or tname in seen:
            continue
        task_to_run = (row.get("Task To Run") or "").strip()
        if not pat.search(f"{tname} {task_to_run}"):
            continue
        seen.add(tname)
        state_raw = (row.get("Status") or row.get("Scheduled Task State") or "Ready").strip()
        last_run_raw = (row.get("Last Run Time") or "").strip()
        next_run_raw = (row.get("Next Run Time") or "").strip()
        sched_type = (row.get("Schedule Type") or "").strip()
        start_time_cfg = (row.get("Start Time") or "").strip()
        repeat_every = (row.get("Repeat: Every") or "").strip()
        try:
            exit_code = int((row.get("Last Result") or "0").strip())
        except ValueError:
            exit_code = 0
        # 267011 = SCHED_S_TASK_HAS_NOT_RUN, 267009 = SCHED_S_TASK_RUNNING
        never_ran = "1999" in last_run_raw or exit_code == 267011 or last_run_raw in ("N/A", "Never", "")
        if exit_code == 0:
            last_status, err = "ok", ""
        elif exit_code == 267009:
            last_status, err = "running", "Task currently running (0x41301)"
        elif exit_code == 267011:
            last_status, err = "never_run", "Not yet triggered by schedule (0x41303)"
        else:
            last_status, err = f"exit_{exit_code}", f"Non-zero exit code: {exit_code} (0x{exit_code & 0xFFFFFFFF:08X})"
        parts = [p for p in (sched_type, start_time_cfg, f"every {repeat_every}" if repeat_every and repeat_every != "Disabled" else "") if p and p != "N/A"]
        rows_out.append(_row(
            "Windows Task Scheduler", "windows", tname, tname.lstrip("\\"), " • ".join(parts) or "Windows Task Scheduler",
            state_raw, state_raw.lower() != "disabled", "-" if never_ran else last_run_raw,
            "-" if next_run_raw in ("N/A", "Disabled", "") else next_run_raw, last_status, exit_code,
            0 if never_ran else 1, task_to_run, err,
        ))
    return rows_out


def parse_launchctl_list(text: str, name_pattern: str = DEFAULT_NAME_PATTERN) -> list[dict[str, Any]]:
    """Parse `launchctl list` (PID<tab>Status<tab>Label)."""
    pat = re.compile(name_pattern, re.IGNORECASE)
    out: list[dict[str, Any]] = []
    for ln in text.splitlines():
        parts = ln.split(None, 2)
        if len(parts) != 3 or parts[0] == "PID":
            continue
        pid, status, label = parts
        if label.startswith("com.apple.") or not pat.search(label):
            continue
        code = int(status) if status.lstrip("-").isdigit() else 0
        running = pid != "-"
        out.append(_row(
            "launchd", "macos", label, label, "launchd", "Running" if running else "Loaded", True, "-", "-",
            "running" if running else ("ok" if code == 0 else f"exit_{code}"), code, 0, label,
            "" if code == 0 else f"Last exit status {code}",
        ))
    return out


def parse_launchd_plist(data: bytes, source_path: str = "") -> dict[str, Any] | None:
    try:
        pl = plistlib.loads(data)
    except Exception:
        return None
    label = str(pl.get("Label") or Path(source_path).stem)
    args = pl.get("ProgramArguments") or ([pl["Program"]] if pl.get("Program") else [])
    if pl.get("StartInterval"):
        sched = f"every {pl['StartInterval']}s"
    elif pl.get("StartCalendarInterval"):
        sched = f"calendar {pl['StartCalendarInterval']}"
    elif pl.get("RunAtLoad") or pl.get("KeepAlive"):
        sched = "at load / keep alive"
    else:
        sched = "launchd"
    return {"label": label, "command": " ".join(str(a) for a in args), "schedule": sched}


def parse_systemd_timers_json(text: str, name_pattern: str = DEFAULT_NAME_PATTERN) -> list[dict[str, Any]]:
    """Parse `systemctl list-timers --all --output=json` (list of {next, left, last, passed, unit, activates})."""
    pat = re.compile(name_pattern, re.IGNORECASE)
    try:
        items = json.loads(text)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for it in items if isinstance(items, list) else []:
        unit = str(it.get("unit") or "")
        activates = str(it.get("activates") or "")
        if not unit or not pat.search(f"{unit} {activates}"):
            continue

        def _us(v: Any) -> str:
            return _fmt_utc_str(int(v) / 1e6) if isinstance(v, (int, float)) and v else "-"

        last, nxt = it.get("last"), it.get("next")
        out.append(_row(
            "systemd timer", "linux", unit, unit, "systemd timer", "Active" if nxt else "Inactive", bool(nxt),
            _us(last), _us(nxt), "ok" if last else "never_run", 0, 1 if last else 0, activates, "",
        ))
    return out


def parse_crontab(text: str, name_pattern: str = DEFAULT_NAME_PATTERN, source: str = "crontab") -> list[dict[str, Any]]:
    pat = re.compile(name_pattern, re.IGNORECASE)
    out: list[dict[str, Any]] = []
    for i, ln in enumerate(text.splitlines()):
        s = ln.strip()
        if not s or s.startswith("#") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", s):
            continue
        if s.startswith("@"):
            parts = s.split(None, 1)
            if len(parts) < 2:
                continue
            sched, cmd = parts
        else:
            parts = s.split(None, 5)
            if len(parts) < 6:
                continue
            sched, cmd = " ".join(parts[:5]), parts[5]
        if not pat.search(cmd):
            continue
        out.append(_row(source, "user", f"cron:{i}", cmd.split()[0].rsplit("/", 1)[-1], sched, "Scheduled", True,
                        "-", "-", "scheduled", 0, 0, cmd[:260], ""))
    return out


def _run(cmd: list[str], timeout: int = 12) -> str:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _fetch_os_schedules_sync(name_pattern: str = DEFAULT_NAME_PATTERN) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if sys.platform == "win32":
        ps_cmd = (
            "Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\\Microsoft\\*' } | "
            "ForEach-Object { "
            "$i = $_ | Get-ScheduledTaskInfo -ErrorAction SilentlyContinue; "
            "$a = ($_.Actions | ForEach-Object { \"$($_.Execute) $($_.Arguments)\".Trim() }) -join '; '; "
            "[PSCustomObject]@{ "
            "HostName = $env:COMPUTERNAME; "
            "TaskName = \"$($_.TaskPath)$($_.TaskName)\"; "
            "'Next Run Time' = if ($i -and $i.NextRunTime) { $i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { 'N/A' }; "
            "Status = [string]$_.State; "
            "'Last Run Time' = if ($i -and $i.LastRunTime) { $i.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { 'N/A' }; "
            "'Last Result' = if ($i) { [string]$i.LastTaskResult } else { '0' }; "
            "'Task To Run' = $a; "
            "'Schedule Type' = 'Scheduled'; "
            "'Start Time' = 'N/A'; "
            "'Repeat: Every' = 'N/A' "
            "} } | ConvertTo-Csv -NoTypeInformation"
        )
        csv_out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd], timeout=8)
        if not csv_out:
            csv_out = _run(["schtasks", "/query", "/fo", "CSV", "/v"], timeout=10)
        rows += parse_schtasks_csv(csv_out, name_pattern)
    else:
        if sys.platform == "darwin":
            rows += parse_launchctl_list(_run(["launchctl", "list"]), name_pattern)
            pat = re.compile(name_pattern, re.IGNORECASE)
            for pf in sorted((Path.home() / "Library" / "LaunchAgents").glob("*.plist")):
                try:
                    info = parse_launchd_plist(pf.read_bytes(), str(pf))
                except OSError:
                    continue
                if info and pat.search(f"{info['label']} {info['command']}") and not any(r["id"] == info["label"] for r in rows):
                    rows.append(_row("launchd", "macos", info["label"], info["label"], info["schedule"], "Configured",
                                     True, "-", "-", "scheduled", 0, 0, info["command"], ""))
        else:
            rows += parse_systemd_timers_json(_run(["systemctl", "--user", "list-timers", "--all", "--output=json"]), name_pattern)
        rows += parse_crontab(_run(["crontab", "-l"]), name_pattern)
    return rows


def _refresh_os_schedules_bg(name_pattern: str) -> None:
    if _OS_CACHE.get("refreshing"):
        return
    _OS_CACHE["refreshing"] = True

    def _worker() -> None:
        try:
            rows = _fetch_os_schedules_sync(name_pattern)
            _OS_CACHE.update(ts=time.time(), key=name_pattern, rows=rows, refreshing=False)
        except Exception:
            _OS_CACHE["refreshing"] = False

    threading.Thread(target=_worker, daemon=True).start()


def query_os_schedules(name_pattern: str = DEFAULT_NAME_PATTERN) -> list[dict[str, Any]]:
    """Collect OS-level scheduled jobs for the current platform (cached 300s, refreshed asynchronously)."""
    now = time.time()
    age = now - float(_OS_CACHE.get("ts") or 0.0)
    if _OS_CACHE.get("key") == name_pattern and age < _OS_CACHE_TTL:
        return list(_OS_CACHE["rows"])
    if _OS_CACHE.get("ts", 0.0) > 0.0 and _OS_CACHE.get("key") == name_pattern:
        _refresh_os_schedules_bg(name_pattern)
        return list(_OS_CACHE["rows"])
    _OS_CACHE.update(ts=now, key=name_pattern)
    _refresh_os_schedules_bg(name_pattern)
    return list(_OS_CACHE["rows"])


def collect_scheduled_tasks_and_crons(
    cutoff_ts: float,
    phoenix_db: Path,
    harness_filter: Optional[str] = None,
    name_pattern: str = DEFAULT_NAME_PATTERN,
) -> dict[str, Any]:
    RAVEN_ROOT = paths.raven_root()
    HERMES_PROFILES = paths.hermes_profiles()
    configured_schedules: list[dict[str, Any]] = []
    recent_runs: list[dict[str, Any]] = []

    # Connect to phoenix_db to enrich runs with spans/tokens/errors where available
    session_telemetry: dict[str, dict[str, Any]] = {}
    heartbeat_turns: list[dict[str, Any]] = []
    outlet_dropped_count = 0
    approval_timeout_count = 0
    try:
        con = sqlite3.connect(f"file:{phoenix_db.as_posix()}?mode=ro", uri=True, timeout=10.0)
        con.row_factory = sqlite3.Row
        srows = con.execute(
            """
            SELECT
                COALESCE(JSON_EXTRACT(s.attributes, '$.child.session_id'), JSON_EXTRACT(s.attributes, '$.session.id'), '') AS sid,
                JSON_EXTRACT(s.attributes, '$.session.id') AS parent_sid,
                t.trace_id,
                COUNT(*) AS spans,
                SUM(CASE WHEN s.span_kind = 'TOOL' THEN 1 ELSE 0 END) AS tool_calls,
                SUM(COALESCE(s.llm_token_count_prompt, JSON_EXTRACT(s.attributes, '$.llm.token_count.prompt'), 0) +
                    COALESCE(s.llm_token_count_completion, JSON_EXTRACT(s.attributes, '$.llm.token_count.completion'), 0)) AS tokens,
                SUM(CASE WHEN s.status_code = 'ERROR' THEN 1 ELSE 0 END) AS errors,
                MAX(CASE WHEN s.status_code = 'ERROR' THEN s.status_message ELSE '' END) AS sample_err
            FROM spans s
            JOIN traces t ON t.id = s.trace_rowid
            WHERE s.start_time >= ?
            GROUP BY sid, parent_sid
            """,
            (ensure_utc(cutoff_ts).strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchall()
        for r in srows:
            for key in (str(r["sid"] or ""), str(r["parent_sid"] or "")):
                if not key:
                    continue
                cur = session_telemetry.setdefault(
                    key,
                    {"trace_id": str(r["trace_id"] or ""), "spans": 0, "tool_calls": 0, "tokens": 0, "errors": 0, "sample_err": ""},
                )
                cur["spans"] += int(r["spans"] or 0)
                cur["tool_calls"] += int(r["tool_calls"] or 0)
                cur["tokens"] += int(r["tokens"] or 0)
                cur["errors"] += int(r["errors"] or 0)
                if r["sample_err"] and not cur["sample_err"]:
                    cur["sample_err"] = str(r["sample_err"])

        # Also query heartbeat individual session.turn spans from phoenix.db
        hb_rows = con.execute(
            """
            SELECT
                s.span_id,
                t.trace_id,
                s.start_time,
                s.end_time,
                s.status_code,
                s.status_message,
                (JULIANDAY(s.end_time) - JULIANDAY(s.start_time)) * 86400.0 AS dur_s,
                JSON_EXTRACT(s.attributes, '$.input.value') AS input_val,
                JSON_EXTRACT(s.attributes, '$.output.value') AS output_val
            FROM spans s
            JOIN traces t ON t.id = s.trace_rowid
            WHERE JSON_EXTRACT(s.attributes, '$.session.id') = 'heartbeat'
              AND s.name = 'session.turn'
              AND s.start_time >= ?
            ORDER BY s.start_time DESC
            LIMIT 60
            """,
            (ensure_utc(cutoff_ts).strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchall()

        hb_all_spans = con.execute(
            """
            SELECT
                s.start_time,
                s.end_time,
                s.span_kind,
                s.status_code,
                s.status_message,
                COALESCE(s.llm_token_count_prompt, 0) + COALESCE(s.llm_token_count_completion, 0) AS tok,
                JSON_EXTRACT(s.attributes, '$.error.class') AS err_class
            FROM spans s
            WHERE JSON_EXTRACT(s.attributes, '$.session.id') = 'heartbeat'
              AND s.start_time >= ?
            """,
            (ensure_utc(cutoff_ts).strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchall()

        err_counts_rows = con.execute(
            """
            SELECT
                JSON_EXTRACT(s.attributes, '$.error.class') AS ec,
                SUM(COALESCE(JSON_EXTRACT(s.attributes, '$.error.count'), 1)) AS cnt
            FROM spans s
            WHERE s.start_time >= ?
              AND JSON_EXTRACT(s.attributes, '$.error.class') IN ('scheduled_delivery_outlet_dropped', 'scheduled_approval_timeout_10s', 'tool_error')
              AND (JSON_EXTRACT(s.attributes, '$.session.id') LIKE '%heartbeat%' OR JSON_EXTRACT(s.attributes, '$.session.id') LIKE '%cron%' OR s.status_message LIKE '%requires user approval%')
            GROUP BY ec
            """,
            (ensure_utc(cutoff_ts).strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchall()
        for er in err_counts_rows:
            if er["ec"] == "scheduled_delivery_outlet_dropped":
                outlet_dropped_count += int(er["cnt"] or 0)
            else:
                approval_timeout_count += int(er["cnt"] or 0)

        for hbr in hb_rows:
            st_s = str(hbr["start_time"])
            et_s = str(hbr["end_time"])
            t_tok = 0
            t_tools = 0
            t_errs = 0
            t_err_msg = ""
            for sp in hb_all_spans:
                if st_s <= str(sp["start_time"]) <= et_s:
                    t_tok += int(sp["tok"] or 0)
                    if sp["span_kind"] == "TOOL":
                        t_tools += 1
                    if sp["status_code"] == "ERROR":
                        t_errs += 1
                        if sp["status_message"] and not t_err_msg:
                            t_err_msg = str(sp["status_message"])
            heartbeat_turns.append(
                {
                    "started_at": st_s[:19],
                    "ended_at": et_s[:19],
                    "source": "Raven Heartbeat",
                    "harness": "raven",
                    "profile": "heartbeat",
                    "job_name": "HEARTBEAT.md (30m tick)",
                    "session_id": "heartbeat",
                    "status": "warn_approval" if t_errs > 0 else "ok",
                    "duration_s": round(max(float(hbr["dur_s"] or 0.0), 0.1), 1),
                    "turns": 1,
                    "tool_calls": t_tools,
                    "tokens": t_tok,
                    "errors": t_errs,
                    "summary": (t_err_msg or str(hbr["output_val"] or hbr["input_val"] or ""))[:220],
                    "trace_id": str(hbr["trace_id"] or make_trace_id("raven", "heartbeat")),
                }
            )
        con.close()
    except Exception:
        pass

    # =========================================================================
    # 1. Raven Configured Crons & Heartbeat
    # =========================================================================
    raven_jobs_file = RAVEN_ROOT / "cron" / "jobs.json"
    raven_job_names: dict[str, str] = {}
    if raven_jobs_file.exists():
        try:
            jdata = json.loads(raven_jobs_file.read_text(encoding="utf-8", errors="replace"))
            for jb in jdata.get("jobs") or []:
                jid = str(jb.get("id") or "")
                jname = str(jb.get("name") or jid)
                raven_job_names[jid] = jname
                sched = jb.get("schedule") or {}
                expr = str(sched.get("expr") or sched.get("kind") or "")
                tz = str(sched.get("tz") or "")
                state_obj = jb.get("state") or {}
                enabled = bool(jb.get("enabled", True))
                last_run_ms = state_obj.get("lastRunAtMs")
                next_run_ms = state_obj.get("nextRunAtMs")
                last_status = str(state_obj.get("lastStatus") or ("scheduled" if enabled else "disabled"))
                last_err = str(state_obj.get("lastError") or "")
                msg = str((jb.get("payload") or {}).get("message") or "")
                configured_schedules.append(
                    {
                        "source": "Raven Cron",
                        "harness": "raven",
                        "profile": str((jb.get("payload") or {}).get("channel") or "tui"),
                        "id": f"cron:{jid}",
                        "name": jname,
                        "schedule": f"{expr} ({tz})" if tz else expr,
                        "state": "Scheduled" if enabled else "Paused",
                        "enabled": enabled,
                        "last_run": _fmt_utc_str(last_run_ms),
                        "next_run": _fmt_utc_str(next_run_ms),
                        "last_status": last_status,
                        "exit_code": 0 if last_status == "ok" else 1,
                        "total_runs": 1 if last_run_ms else 0,
                        "command_or_prompt": msg[:260],
                        "last_error": last_err,
                        "trace_id": make_trace_id("raven", f"cron:{jid}"),
                    }
                )
        except Exception:
            pass

    hb_md = RAVEN_ROOT / "workspace" / "HEARTBEAT.md"
    if hb_md.exists():
        try:
            hb_text = hb_md.read_text(encoding="utf-8", errors="replace")
            active_lines = []
            in_active = False
            for ln in hb_text.splitlines():
                if ln.strip().startswith("## Active Tasks"):
                    in_active = True
                    continue
                if ln.strip().startswith("## ") and in_active:
                    break
                if in_active and ln.strip() and not ln.strip().startswith("<!--"):
                    active_lines.append(ln.strip())
            hb_prompt = " ".join(active_lines)[:260] if active_lines else "Periodic heartbeat check"
            last_hb_run = heartbeat_turns[0]["started_at"] if heartbeat_turns else "-"
            next_hb_run = "-"
            if heartbeat_turns:
                try:
                    last_dt = ensure_utc(heartbeat_turns[0]["started_at"])
                    next_hb_run = (last_dt + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
            configured_schedules.append(
                {
                    "source": "Raven Heartbeat",
                    "harness": "raven",
                    "profile": "heartbeat",
                    "id": "heartbeat",
                    "name": "HEARTBEAT.md (Active Tasks)",
                    "schedule": "every 30m (interval)",
                    "state": "Active" if active_lines else "Idle",
                    "enabled": bool(active_lines),
                    "last_run": last_hb_run,
                    "next_run": next_hb_run,
                    "last_status": heartbeat_turns[0]["status"] if heartbeat_turns else "ok",
                    "exit_code": 0,
                    "total_runs": len(heartbeat_turns),
                    "command_or_prompt": hb_prompt,
                    "last_error": f"{approval_timeout_count} non-interactive approval timeouts, {outlet_dropped_count} dropped CLI outlet msgs" if (approval_timeout_count or outlet_dropped_count) else "",
                    "trace_id": make_trace_id("raven", "heartbeat"),
                }
            )
        except Exception:
            pass

    # Parse Raven cron session JSONL files into individual runs
    for cf in sorted(glob.glob(str(RAVEN_ROOT / "workspace" / "sessions" / "cron" / "*.jsonl"))):
        try:
            jid = Path(cf).stem
            lines = Path(cf).read_text(encoding="utf-8", errors="replace").splitlines()
            # Group into runs whenever a 'user' message with [Scheduled Task] appears
            runs_in_file: list[dict[str, Any]] = []
            cur_run: Optional[dict[str, Any]] = None
            for ln in lines:
                if not ln.strip():
                    continue
                rec = json.loads(ln)
                if rec.get("_type") == "metadata":
                    continue
                role = rec.get("role")
                ts = rec.get("timestamp")
                content = str(rec.get("content") or "")
                clean_content = re.sub(r"^\[BEGIN UNTRUSTED [^\]]+\]\s*", "", content).strip()
                if role == "user" and ("[Scheduled Task]" in content or cur_run is None):
                    if cur_run:
                        runs_in_file.append(cur_run)
                    m_title = re.search(r"Task '([^']+)'", content)
                    jtitle = m_title.group(1) if m_title else raven_job_names.get(jid, f"cron:{jid}")
                    cur_run = {
                        "jid": jid,
                        "job_name": jtitle,
                        "started_at_raw": ts,
                        "ended_at_raw": ts,
                        "turns": 1,
                        "tool_calls": 0,
                        "errors": 0,
                        "err_msgs": [],
                        "last_output": "",
                    }
                elif cur_run is not None:
                    if ts:
                        cur_run["ended_at_raw"] = ts
                    if role == "assistant":
                        cur_run["turns"] += 1
                        tcs = rec.get("tool_calls") or []
                        cur_run["tool_calls"] += len(tcs)
                        if clean_content:
                            cur_run["last_output"] = clean_content
                    elif role == "tool":
                        if "Error:" in clean_content or "requires user approval" in clean_content or "cannot schedule new jobs" in clean_content:
                            cur_run["errors"] += 1
                            cur_run["err_msgs"].append(clean_content[:200])
            if cur_run:
                runs_in_file.append(cur_run)

            sess_key = f"cron:{jid}"
            tele = session_telemetry.get(sess_key, {})
            total_file_tok = int(tele.get("tokens") or 0)
            per_run_tok = total_file_tok // max(len(runs_in_file), 1)

            for rn in runs_in_file:
                st_dt = ensure_utc(rn["started_at_raw"])
                en_dt = ensure_utc(rn["ended_at_raw"])
                if en_dt.timestamp() < cutoff_ts and st_dt.timestamp() < cutoff_ts:
                    continue
                dur_s = round(max((en_dt - st_dt).total_seconds(), 0.1), 1)
                errs = int(rn["errors"])
                summary_txt = rn["err_msgs"][0] if rn["err_msgs"] else rn["last_output"]
                recent_runs.append(
                    {
                        "started_at": st_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "ended_at": en_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "source": "Raven Cron",
                        "harness": "raven",
                        "profile": "cron",
                        "job_name": f"{rn['job_name']} ({jid})",
                        "session_id": sess_key,
                        "status": "warn_blocked" if errs > 0 else "ok",
                        "duration_s": dur_s,
                        "turns": rn["turns"],
                        "tool_calls": rn["tool_calls"],
                        "tokens": per_run_tok,
                        "errors": errs,
                        "summary": summary_txt[:220].replace("\n", " "),
                        "trace_id": make_trace_id("raven", sess_key),
                    }
                )
        except Exception:
            pass

    recent_runs.extend(heartbeat_turns)

    # =========================================================================
    # 2. Hermes Crons across all discovered profiles (jobs.json + executions.db + state.db + usage_audit.jsonl)
    # =========================================================================
    hermes_job_names: dict[str, str] = {}
    hermes_audit_by_job: dict[str, list[dict[str, Any]]] = {}

    for prof_name, prof_dir in HERMES_PROFILES:
        cron_dir = prof_dir / "cron"
        if not cron_dir.exists():
            continue

        # Read usage_audit.jsonl if present
        audit_file = cron_dir / "usage_audit.jsonl"
        if audit_file.exists():
            try:
                for ln in audit_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not ln.strip():
                        continue
                    arec = json.loads(ln)
                    jid = str(arec.get("job_id") or "")
                    if jid:
                        hermes_audit_by_job.setdefault(jid, []).append(arec)
            except Exception:
                pass

        # Read jobs.json if present
        jobs_file = cron_dir / "jobs.json"
        if jobs_file.exists():
            try:
                jdata = json.loads(jobs_file.read_text(encoding="utf-8", errors="replace"))
                for jb in jdata.get("jobs") or []:
                    jid = str(jb.get("id") or "")
                    jname = str(jb.get("name") or jid)
                    hermes_job_names[jid] = jname
                    enabled = bool(jb.get("enabled", False))
                    state_str = str(jb.get("state") or ("scheduled" if enabled else "paused"))
                    sched_disp = str(jb.get("schedule_display") or (jb.get("schedule") or {}).get("expr") or "")
                    last_run_at = jb.get("last_run_at")
                    next_run_at = jb.get("next_run_at")
                    last_status = str(jb.get("last_status") or "-")
                    repeat_completed = int((jb.get("repeat") or {}).get("completed") or 0)
                    last_err = str(jb.get("last_error") or ((jb.get("last_failure") or {}).get("detail") or ""))
                    script_or_prompt = str(jb.get("script") or jb.get("prompt") or "")[:240]
                    configured_schedules.append(
                        {
                            "source": "Hermes Cron",
                            "harness": "hermes",
                            "profile": prof_name,
                            "id": jid,
                            "name": jname,
                            "schedule": sched_disp,
                            "state": state_str.capitalize(),
                            "enabled": enabled,
                            "last_run": _fmt_utc_str(last_run_at),
                            "next_run": _fmt_utc_str(next_run_at),
                            "last_status": last_status,
                            "exit_code": 0 if last_status == "ok" else (1 if last_status == "error" else 0),
                            "total_runs": repeat_completed,
                            "command_or_prompt": script_or_prompt.replace("\n", " "),
                            "last_error": last_err.splitlines()[0][:200] if last_err else "",
                            "trace_id": make_trace_id("hermes", f"cron:{prof_name}:{jid}"),
                        }
                    )
            except Exception:
                pass

        # Read executions.db
        exec_db = cron_dir / "executions.db"
        exec_by_id: dict[str, dict[str, Any]] = {}
        if exec_db.exists():
            try:
                econ = _connect_readonly(exec_db)
                try:
                    erows = econ.execute(
                        """
                        SELECT id, job_id, source, status, claimed_at, started_at, finished_at, error, delivery_outcome, scheduled_instant
                        FROM executions
                        ORDER BY COALESCE(started_at, claimed_at, '') DESC
                        LIMIT 50
                        """
                    ).fetchall()
                    for er in erows:
                        st_raw = er["started_at"] or er["claimed_at"]
                        fn_raw = er["finished_at"] or st_raw
                        st_dt = ensure_utc(st_raw)
                        fn_dt = ensure_utc(fn_raw)
                        if fn_dt.timestamp() < cutoff_ts and st_dt.timestamp() < cutoff_ts:
                            continue
                        jid = str(er["job_id"] or "")
                        jname = hermes_job_names.get(jid, f"cron:{jid}")
                        status = str(er["status"] or "unknown")
                        err_txt = str(er["error"] or "")
                        dur_s = round(max((fn_dt - st_dt).total_seconds(), 0.1), 1)

                        # Match usage_audit token count if close in time
                        matched_tok = 0
                        for aud in hermes_audit_by_job.get(jid, []):
                            try:
                                aud_dt = ensure_utc(aud.get("ts"))
                                if abs((aud_dt - fn_dt).total_seconds()) < 180:
                                    matched_tok = int(aud.get("total_tokens") or 0)
                                    break
                            except Exception:
                                pass

                        exec_by_id[str(er["id"])] = {
                            "started_at": st_dt.strftime("%Y-%m-%d %H:%M:%S"),
                            "ended_at": fn_dt.strftime("%Y-%m-%d %H:%M:%S"),
                            "source": f"Hermes Cron ({er['source'] or 'scheduler'})",
                            "harness": "hermes",
                            "profile": prof_name,
                            "job_name": f"{jname} ({jid})",
                            "session_id": f"cron:{jid}",
                            "status": status,
                            "duration_s": dur_s,
                            "turns": 1,
                            "tool_calls": 0,
                            "tokens": matched_tok,
                            "errors": 1 if (status in ("failed", "error", "unknown") or err_txt) else 0,
                            "summary": (err_txt.splitlines()[0] if err_txt else f"Execution {er['id'][:8]} ({er['delivery_outcome'] or status})")[:220],
                            "trace_id": make_trace_id("hermes", f"cron:{prof_name}:{jid}"),
                        }
                finally:
                    econ.close()
            except Exception:
                pass

        # Read cron sessions from state.db
        state_db_file = prof_dir / "state.db"
        if state_db_file.exists():
            try:
                scon = _connect_readonly(state_db_file)
                try:
                    crows = scon.execute(
                        """
                        SELECT id, source, created_source, started_at, ended_at, last_activity_at,
                               end_reason, message_count, tool_call_count, input_tokens, output_tokens,
                               cache_read_tokens, title
                        FROM sessions
                        WHERE (source = 'cron' OR created_source = 'cron' OR end_reason LIKE '%cron%' OR id LIKE 'cron_%')
                          AND COALESCE(last_activity_at, ended_at, started_at, 0) >= ?
                        ORDER BY COALESCE(started_at, 0) DESC
                        LIMIT 50
                        """,
                        (cutoff_ts,),
                    ).fetchall()
                    for cr in crows:
                        sid = str(cr["id"] or "")
                        st_sec = float(cr["started_at"] or cr["last_activity_at"] or cutoff_ts)
                        en_sec = float(cr["ended_at"] or cr["last_activity_at"] or st_sec)
                        st_dt = ensure_utc(st_sec)
                        en_dt = ensure_utc(en_sec)
                        dur_s = round(max(en_sec - st_sec, 0.1), 1)
                        tok = int(cr["input_tokens"] or 0) + int(cr["cache_read_tokens"] or 0) + int(cr["output_tokens"] or 0)
                        title = str(cr["title"] or sid)
                        end_reason = str(cr["end_reason"] or "running")
                        tele = session_telemetry.get(sid, {})
                        errs = int(tele.get("errors") or 0)
                        recent_runs.append(
                            {
                                "started_at": st_dt.strftime("%Y-%m-%d %H:%M:%S"),
                                "ended_at": en_dt.strftime("%Y-%m-%d %H:%M:%S"),
                                "source": "Hermes Cron Session",
                                "harness": "hermes",
                                "profile": prof_name,
                                "job_name": title.split("•")[0].strip() if "•" in title else title[:48],
                                "session_id": sid,
                                "status": "ok" if end_reason == "cron_complete" and errs == 0 else (end_reason if end_reason != "cron_complete" else "warn"),
                                "duration_s": dur_s,
                                "turns": int(cr["message_count"] or 1) // 2 or 1,
                                "tool_calls": int(cr["tool_call_count"] or tele.get("tool_calls") or 0),
                                "tokens": tok or int(tele.get("tokens") or 0),
                                "errors": errs,
                                "summary": f"{title} ({end_reason})",
                                "trace_id": str(tele.get("trace_id") or make_trace_id("hermes", sid)),
                            }
                        )
                finally:
                    scon.close()
            except Exception:
                pass

        # Add executions from executions.db that were failed/aborted (e.g. GatewayUnavailableError, RuntimeStoreError, script failure) or didn't spawn a state.db session
        for eid, ex_item in exec_by_id.items():
            # Check if we already have a state.db session at the exact same minute with tokens
            already_covered = False
            if ex_item["status"] == "completed":
                for rr in recent_runs:
                    if rr["harness"] == "hermes" and rr["profile"] == prof_name and rr["started_at"][:15] == ex_item["started_at"][:15]:
                        already_covered = True
                        if ex_item["tokens"] > rr["tokens"]:
                            rr["tokens"] = ex_item["tokens"]
                        break
            if not already_covered:
                recent_runs.append(ex_item)

    # =========================================================================
    # 3. OS scheduler (Task Scheduler / launchd / systemd / crontab)
    # =========================================================================
    configured_schedules.extend(query_os_schedules(name_pattern))

    # Filter by harness if requested
    if harness_filter and harness_filter != "all":
        configured_schedules = [
            s for s in configured_schedules if s["harness"] == harness_filter or (harness_filter == "raven" and s["harness"] == "agent-tap")
        ]
        recent_runs = [r for r in recent_runs if r["harness"] == harness_filter]

    recent_runs.sort(key=lambda x: x.get("started_at") or "", reverse=True)

    enabled_count = sum(1 for s in configured_schedules if s.get("enabled"))
    failed_schedules = sum(
        1
        for s in configured_schedules
        if str(s.get("last_status")).lower() not in ("ok", "ready", "running", "never_run", "scheduled", "-")
        or (s.get("exit_code") not in (0, 267009, 267011, None))
    )
    run_errors = sum(1 for r in recent_runs if int(r.get("errors") or 0) > 0 or str(r.get("status")) not in ("ok", "completed"))

    return {
        "summary": {
            "total_configured": len(configured_schedules),
            "enabled_count": enabled_count,
            "failed_schedules": failed_schedules,
            "recent_runs_count": len(recent_runs),
            "recent_runs_with_errors": run_errors,
            "outlet_dropped_count": outlet_dropped_count,
            "approval_timeout_count": approval_timeout_count,
        },
        "configured_schedules": configured_schedules,
        "recent_runs": recent_runs[:150],
    }


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10.0)
    con.row_factory = sqlite3.Row
    return con
