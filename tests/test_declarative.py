import json
import shutil
import time
from pathlib import Path

import pytest

from agent_tap.adapters.declarative import DeclarativeAdapter, load_declarative_adapters, validate_file

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "adapters"
PKG_EXAMPLES = Path(__file__).resolve().parents[1] / "src" / "agent_tap" / "examples"

from conftest import write_jsonl


def _install(tmp_path, name):
    d = tmp_path / "adapters"
    d.mkdir(exist_ok=True)
    shutil.copy(EXAMPLES / name, d / name)
    return d


@pytest.mark.parametrize("name", ["aider.toml", "cline.toml"])
def test_examples_valid_and_identical_in_package(name):
    assert validate_file(EXAMPLES / name) == []
    assert (EXAMPLES / name).read_bytes() == (PKG_EXAMPLES / name).read_bytes()


def test_validate_reports_errors(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('name = "x"\nkind = "csv"\n', encoding="utf-8")
    errs = validate_file(bad)
    assert any("kind" in e for e in errs) and any("[source]" in e for e in errs) and any("[map]" in e for e in errs)
    assert validate_file(tmp_path / "missing.toml")[0].startswith("cannot parse")


def test_aider_jsonl_incremental(home, cfg, run, tmp_path):
    log = home / ".aider" / "analytics.jsonl"
    now = time.time()
    write_jsonl(log, [
        {"time": now, "event": "message_send", "user_id": "u-1",
         "properties": {"main_model": "gpt-x", "prompt_tokens": 11, "completion_tokens": 22}},
        {"time": now, "event": "exception", "user_id": "u-1", "properties": {"error": "boom"}},
    ])
    [ad] = load_declarative_adapters(_install(tmp_path, "aider.toml"))
    assert ad.name == "aider" and ad.discover(None)
    res, sp = run(ad, 0)
    assert res == {"aider": (2, 1)}
    assert [s["name"] for s in sp] == ["aider.message_send", "aider.exception"]
    assert sp[0]["attrs"]["llm"]["token_count"] == {"prompt": 11, "completion": 22}
    assert sp[1]["status"] == "ERROR"
    res2, sp2 = run(ad, 0)  # watermark: nothing new
    assert res2 == {"aider": (0, 0)} and len(sp2) == 2
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": now, "event": "later", "user_id": "u-1", "properties": {}}) + "\n")
    res3, sp3 = run(ad, 0)
    assert res3 == {"aider": (1, 0)} and len(sp3) == 3


def test_cline_json_array_with_where_filter(home, cfg, run, tmp_path, monkeypatch):
    tdir = home / ".config" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "tasks" / "task-123"
    tdir.mkdir(parents=True)
    now_ms = int(time.time() * 1000)
    (tdir / "ui_messages.json").write_text(json.dumps([
        {"ts": now_ms, "type": "say", "say": "text", "text": "private cline text"},
        {"ts": now_ms + 1, "type": "ask", "ask": "tool", "text": "skipped"},
    ]), encoding="utf-8")
    [ad] = load_declarative_adapters(_install(tmp_path, "cline.toml"))
    res, sp = run(ad, 0, store_prompts=True)
    assert res == {"cline": (1, 0)}
    assert sp[0]["name"] == "cline.text" and sp[0]["attrs"]["session"]["id"] == "task-123"
    assert sp[0]["attrs"]["output"]["value"] == "private cline text"


def test_declarative_honors_store_prompt_text_off(home, cfg, run, tmp_path):
    tdir = home / ".config" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "tasks" / "t"
    tdir.mkdir(parents=True)
    (tdir / "ui_messages.json").write_text(json.dumps([{"ts": int(time.time() * 1000), "type": "say", "say": "text", "text": "secret words"}]),
                                           encoding="utf-8")
    [ad] = load_declarative_adapters(_install(tmp_path, "cline.toml"))
    _, sp = run(ad, 0, store_prompts=False)
    assert "secret words" not in json.dumps(sp)


def test_sqlite_declarative(tmp_path, home, cfg, run):
    import sqlite3

    db = tmp_path / "h.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ev (id INTEGER PRIMARY KEY, ts REAL, sess TEXT, what TEXT, err TEXT)")
    con.execute("INSERT INTO ev VALUES (1, ?, 's1', 'start', NULL)", (time.time(),))
    con.execute("INSERT INTO ev VALUES (2, ?, 's1', 'fail', 'bad')", (time.time(),))
    con.commit()
    con.close()
    toml = tmp_path / "x.toml"
    toml.write_text(
        'name = "mine"\nkind = "sqlite"\n[source]\ndb = "%s"\nrowid_column = "id"\nquery = "SELECT * FROM ev WHERE id > :last_rowid ORDER BY id"\n'
        '[map]\ntimestamp = "ts"\nname = "what"\nsession_id = "sess"\nid = "id"\nerror = "err"\n' % db.as_posix(), encoding="utf-8")
    assert validate_file(toml) == []
    ad = DeclarativeAdapter(__import__("tomllib").loads(toml.read_text()), toml)
    res, sp = run(ad, 0)
    assert res == {"mine": (2, 1)}
    assert run(ad, 0)[0] == {"mine": (0, 0)}
