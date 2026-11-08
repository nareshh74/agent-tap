import json
import plistlib

from agent_tap.platform import scheduler as sch

SCHTASKS = "\n".join([
    r'"HostName","TaskName","Next Run Time","Status","Last Run Time","Last Result","Task To Run","Schedule Type","Start Time","Repeat: Every"',
    r'"PC","\agent-tap","N/A","Ready","11/8/2026 9:00:00 AM","0","C:\x\agent-tap.exe run","At logon time","N/A","N/A"',
    r'"PC","\Microsoft\Windows\Foo","N/A","Ready","N/A","0","agent.exe","Daily","N/A","N/A"',
    r'"PC","\Unrelated","N/A","Ready","N/A","0","notepad.exe","Daily","N/A","N/A"',
    r'"PC","\hermes-sync","11/9/2026 1:00:00 AM","Disabled","11/30/1999 12:00:00 AM","267011","hermes sync","Daily","1:00:00 AM","1 Hour(s)"',
])


def test_schtasks():
    rows = sch.parse_schtasks_csv(SCHTASKS)
    assert [r["name"] for r in rows] == ["agent-tap", "hermes-sync"]
    assert rows[0]["harness"] == "agent-tap" and rows[0]["last_status"] == "ok"
    assert rows[1]["harness"] == "hermes" and rows[1]["last_status"] == "never_run" and rows[1]["enabled"] is False


def test_launchctl():
    out = "PID\tStatus\tLabel\n123\t0\tio.agent-tap.daemon\n-\t1\tcom.apple.Safari\n-\t78\tcom.example.codex-sync\n"
    rows = sch.parse_launchctl_list(out)
    assert [r["id"] for r in rows] == ["io.agent-tap.daemon", "com.example.codex-sync"]
    assert rows[0]["last_status"] == "running" and rows[1]["exit_code"] == 78


def test_launchd_plist():
    data = plistlib.dumps({"Label": "io.agent-tap.daemon", "ProgramArguments": ["/bin/agent-tap", "run"], "StartInterval": 60})
    assert sch.parse_launchd_plist(data) == {"label": "io.agent-tap.daemon", "command": "/bin/agent-tap run", "schedule": "every 60s"}


def test_systemd_timers():
    text = json.dumps([
        {"next": 1_800_000_000_000_000, "last": 1_700_000_000_000_000, "unit": "agent-tap-prune.timer", "activates": "agent-tap-prune.service"},
        {"next": 0, "last": 0, "unit": "fstrim.timer", "activates": "fstrim.service"},
    ])
    rows = sch.parse_systemd_timers_json(text)
    assert len(rows) == 1 and rows[0]["name"] == "agent-tap-prune.timer" and rows[0]["enabled"] is True
    assert sch.parse_systemd_timers_json("not json") == []


def test_crontab():
    text = "# c\nPATH=/bin\n*/5 * * * * /usr/bin/claude-sync --x\n@daily /opt/agent-tap/prune\n0 1 * * * backup.sh\n"
    rows = sch.parse_crontab(text)
    assert [r["schedule"] for r in rows] == ["*/5 * * * *", "@daily"]
    assert rows[0]["command_or_prompt"] == "/usr/bin/claude-sync --x"


def test_collect_tolerates_empty_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "l"))
    monkeypatch.setattr(sch, "query_os_schedules", lambda *_a, **_k: [])
    out = sch.collect_scheduled_tasks_and_crons(0.0, tmp_path / "missing.db")
    assert out["summary"]["total_configured"] == 0 and out["recent_runs"] == []
