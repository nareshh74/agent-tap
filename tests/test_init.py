import tomllib

import pytest

from agent_tap import onboarding
from agent_tap.adapters.base import Context, load_adapters
from agent_tap.cli import main
from agent_tap.core.config import load_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(tmp_path))
    for var in ("LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "HERMES_HOME", "CODEX_HOME",
                "AGENT_TAP_STORE_PROMPTS", "AGENT_TAP_OMNIROUTE_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_TAP_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    monkeypatch.setattr(onboarding, "can_prompt", lambda: False)
    monkeypatch.setattr("agent_tap.adapters.omniroute.OmniRouteAdapter._docker", staticmethod(lambda: None))
    monkeypatch.setattr("agent_tap.adapters.service.http_probe", lambda *a, **k: (None, 0.0, "unreachable", "refused"))
    monkeypatch.setattr("agent_tap.adapters.service.docker_available", lambda: False)
    monkeypatch.setattr("agent_tap.adapters.service.process_running", lambda n: False)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    return tmp_path


def _cfg(home):
    return load_config(None, {"adapters_dir": home / "adapters", "spans_db": home / "s.db", "state_db": home / "st.db"})


def test_detect_marks_only_existing_data(home):
    found = onboarding.detect_harnesses(_cfg(home))
    assert found["codex"] and found["claude-code"]
    assert not found["hermes"] and not found["raven"] and not found["copilot-cli"]
    assert {"aider", "cline"} <= set(found)


def _responder(**by_key):
    """Answer prompts by a keyword in the question text; unknown questions get Enter."""
    def ask(q: str) -> str:
        for key, val in by_key.items():
            if key.replace("_", " ") in q.lower():
                return val
        return ""
    return ask


def test_interactive_selection_and_prompt_choice(home):
    names = list(onboarding.detect_harnesses(_cfg(home)))
    pick = f"{names.index('claude-code') + 1}, codex"
    path = onboarding.run_init(_cfg(home), ask=_responder(enable=pick, store="y"), out=lambda _: None)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["enabled_services"] == ["claude-code", "codex"]
    assert data["store_prompt_text"] is True


def test_interactive_default_enter_preselects_detected(home):
    path = onboarding.run_init(_cfg(home), ask=lambda _: "", out=lambda _: None)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert set(data["enabled_services"]) == {"claude-code", "codex"}
    assert data["store_prompt_text"] is False


def test_custom_omniroute_path_recorded(home):
    names = list(onboarding.detect_harnesses(_cfg(home)))
    ask = _responder(enable=str(names.index("omniroute") + 1), omniroute="/data/omni")
    path = onboarding.run_init(_cfg(home), ask=ask, out=lambda _: None)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["paths"]["omniroute"] == "/data/omni"
    assert data["enabled_services"] == ["omniroute"]


def test_init_flags_never_prompt(home, capsys):
    rc = main(["--spans-db", str(home / "s.db"), "--state-db", str(home / "st.db"), "init", "--harnesses", "codex,aider", "--yes"])
    assert rc == 0
    cfg_path = home / "cfg" / "config.toml"
    data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["enabled_services"] == ["codex", "aider"] and data["store_prompt_text"] is False
    assert (load_config(None).adapters_dir / "aider.toml").exists()


def test_init_non_interactive_uses_detected_and_unknown_harness_errors(home, capsys):
    assert main(["init", "--non-interactive"]) == 0
    data = tomllib.loads((home / "cfg" / "config.toml").read_text(encoding="utf-8"))
    assert set(data["enabled_services"]) == {"claude-code", "codex"}
    assert main(["init", "--harnesses", "nope"]) == 2


def test_init_preserves_existing_keys_and_backs_up(home):
    cfg_path = home / "cfg" / "config.toml"
    cfg_path.parent.mkdir()
    cfg_path.write_text("port = 7777\nstore_prompt_text = true\n", encoding="utf-8")
    assert main(["init", "--non-interactive", "--harnesses", "codex"]) == 0
    data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["port"] == 7777 and data["store_prompt_text"] is True
    assert (cfg_path.with_name("config.toml.bak")).exists()


def test_enabled_harnesses_filters_adapters(home):
    names = lambda cfg: {a.name for a in load_adapters(cfg)}  # noqa: E731
    cfg = _cfg(home)
    assert {"raven", "hermes", "codex"} <= names(cfg)
    cfg.enabled_harnesses = ["codex"]
    assert names(cfg) == {"codex"}
    cfg.enabled_harnesses = []
    assert names(cfg) == set()


def test_legacy_adapters_key_still_filters(home):
    f = home / "legacy.toml"
    f.write_text('adapters = ["codex"]\n', encoding="utf-8")
    assert load_config(f).enabled_harnesses == ["codex"]


def test_run_does_not_prompt_without_tty(home, capsys):
    rc = main(["--spans-db", str(home / "s.db"), "--state-db", str(home / "st.db"), "run", "--once"])
    assert rc == 0 and not (home / "cfg" / "config.toml").exists()


def test_service_install_with_harnesses_writes_config_first(home, monkeypatch):
    called = {}
    monkeypatch.setattr("agent_tap.platform.service.service_main", lambda *a, **k: (called.setdefault("ok", True) and 0, ""))
    assert main(["service", "install", "--harnesses", "codex"]) == 0
    assert called and tomllib.loads((home / "cfg" / "config.toml").read_text(encoding="utf-8"))["enabled_services"] == ["codex"]
