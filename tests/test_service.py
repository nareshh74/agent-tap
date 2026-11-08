import plistlib
import xml.etree.ElementTree as ET

import pytest

from agent_tap.platform import service

ARGV = ["/usr/local/bin/agent-tap", "run", "--watch"]


def test_render_linux():
    t = service.render_unit("linux", ARGV)
    assert "ExecStart=/usr/local/bin/agent-tap run --watch" in t and "WantedBy=default.target" in t


def test_render_macos():
    pl = plistlib.loads(service.render_unit("macos", ARGV).encode())
    assert pl["Label"] == "io.agent-tap.daemon" and pl["ProgramArguments"] == ARGV and pl["KeepAlive"] is True


def test_render_windows():
    xml = service.render_unit("windows", [r"C:\Program Files\agent-tap.exe", "run", "--watch"])
    root = ET.fromstring(xml.split("?>", 1)[1])
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:Command", ns).text == r"C:\Program Files\agent-tap.exe"
    assert root.find(".//t:Arguments", ns).text == "run --watch"
    assert root.find(".//t:LogonTrigger", ns) is not None


def test_render_unknown_os():
    with pytest.raises(ValueError):
        service.render_unit("beos", ARGV)


def test_service_main_render_and_foreign_os():
    code, text = service.service_main("render", "linux", argv=ARGV)
    assert code == 0 and "[Service]" in text
    foreign = "macos" if service.current_os() != "macos" else "linux"
    assert service.service_main("install", foreign)[0] == 2


def test_install_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    code, text = service.service_main("install", dry_run=True, argv=ARGV)
    assert code == 0 and "[dry-run]" in text and not list(tmp_path.iterdir())
