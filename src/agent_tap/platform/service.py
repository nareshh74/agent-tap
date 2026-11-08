"""Install agent-tap as a per-user background service on Windows, macOS and Linux."""
from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "agent-tap"
LAUNCHD_LABEL = "io.agent-tap.daemon"
SYSTEMD_UNIT = "agent-tap.service"
OS_NAMES = ("windows", "macos", "linux")
DEFAULT_ARGS = ["run", "--watch"]


def current_os() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def resolve_command(args: list[str] | None = None) -> list[str]:
    """argv that starts the daemon: the `agent-tap` script if on PATH, else `python -m agent_tap`."""
    exe = shutil.which("agent-tap")
    base = [exe] if exe else [sys.executable, "-m", "agent_tap"]
    return base + list(args if args is not None else DEFAULT_ARGS)


def unit_path(os_name: str) -> Path | None:
    home = Path.home()
    if os_name == "linux":
        return home / ".config" / "systemd" / "user" / SYSTEMD_UNIT
    if os_name == "macos":
        return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    return None


def _quote_win(argv: list[str]) -> str:
    return " ".join(f'"{a}"' if " " in a else a for a in argv)


def render_unit(os_name: str, argv: list[str] | None = None) -> str:
    """Render the service definition text for os_name (systemd unit, launchd plist, or Task Scheduler XML)."""
    argv = argv or resolve_command()
    if os_name == "linux":
        exec_start = " ".join(f'"{a}"' if " " in a else a for a in argv)
        return (
            "[Unit]\nDescription=agent-tap: local AI agent session analytics\nAfter=network.target\n\n"
            f"[Service]\nType=simple\nExecStart={exec_start}\nRestart=on-failure\nRestartSec=5\n"
            "Environment=PYTHONUTF8=1\n\n[Install]\nWantedBy=default.target\n"
        )
    if os_name == "macos":
        log = str(Path.home() / "Library" / "Logs" / "agent-tap.log")
        return plistlib.dumps({
            "Label": LAUNCHD_LABEL, "ProgramArguments": argv, "RunAtLoad": True, "KeepAlive": True,
            "StandardOutPath": log, "StandardErrorPath": log,
            "EnvironmentVariables": {"PYTHONUTF8": "1"},
        }).decode("utf-8")
    if os_name == "windows":
        cmd, *rest = argv
        return (
            '<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            "  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>\n"
            "  <Principals><Principal id=\"Author\"><LogonType>InteractiveToken</LogonType>"
            "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n"
            "  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
            "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
            "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>"
            "<RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>"
            "<Hidden>true</Hidden></Settings>\n"
            f"  <Actions Context=\"Author\"><Exec><Command>{escape(cmd)}</Command>"
            f"<Arguments>{escape(_quote_win(rest))}</Arguments></Exec></Actions>\n</Task>\n"
        )
    raise ValueError(f"unknown os: {os_name!r} (expected one of {OS_NAMES})")


def _sh(cmd: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def service_main(action: str, os_name: str | None = None, dry_run: bool = False, argv: list[str] | None = None) -> tuple[int, str]:
    """Run install|status|uninstall|render. Returns (exit_code, text). Only `render` may target a foreign OS."""
    target = os_name or current_os()
    if target not in OS_NAMES:
        return 2, f"unknown os {target!r}"
    if action == "render":
        return 0, render_unit(target, argv)
    if target != current_os():
        return 2, f"cannot {action} a {target} service on {current_os()}; use `render --os {target}`"

    path = unit_path(target)
    if action == "install":
        text = render_unit(target, argv)
        if dry_run:
            return 0, f"[dry-run] would write {path or 'Task Scheduler task ' + TASK_NAME}\n{text}"
        if target == "windows":
            xml = Path.home() / "AppData" / "Local" / "agent-tap" / "task.xml"
            xml.parent.mkdir(parents=True, exist_ok=True)
            xml.write_text(text, encoding="utf-16")
            code, out = _sh(["schtasks", "/create", "/tn", TASK_NAME, "/xml", str(xml), "/f"])
            if code == 0:
                _sh(["schtasks", "/run", "/tn", TASK_NAME])
            return code, out or f"installed task {TASK_NAME}"
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if target == "linux":
            code, out = _sh(["systemctl", "--user", "daemon-reload"])
            if code == 0:
                code, out = _sh(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT])
        else:
            _sh(["launchctl", "unload", str(path)])
            code, out = _sh(["launchctl", "load", "-w", str(path)])
        return code, out or f"installed {path}"
    if action == "status":
        if target == "windows":
            return _sh(["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST"])
        if target == "linux":
            return _sh(["systemctl", "--user", "status", SYSTEMD_UNIT, "--no-pager"])
        return _sh(["launchctl", "list", LAUNCHD_LABEL])
    if action == "uninstall":
        if dry_run:
            return 0, f"[dry-run] would remove {path or 'task ' + TASK_NAME}"
        if target == "windows":
            _sh(["schtasks", "/end", "/tn", TASK_NAME])
            return _sh(["schtasks", "/delete", "/tn", TASK_NAME, "/f"])
        assert path is not None
        if target == "linux":
            _sh(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
        else:
            _sh(["launchctl", "unload", str(path)])
        path.unlink(missing_ok=True)
        if target == "linux":
            _sh(["systemctl", "--user", "daemon-reload"])
        return 0, f"removed {path}"
    return 2, f"unknown action {action!r}"
