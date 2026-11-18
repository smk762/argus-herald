import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from argus_herald import clips
from argus_herald.cli import main
from argus_herald.models import Post
from argus_herald.platforms import DiscordPublisher
from argus_herald.store import Store
from argus_herald.workflow import ReviewError, compose, log_broadcast

# The header of a list kept by hand before the registry: other names, and columns it doesn't keep.
HAND_KEPT = (
    "clip_id,demo,package,title,format,target_len_s,status,recorded_on,pkg_version_recorded,"
    "demo_repo,demo_commit_recorded,file,stale_if,notes\n"
    "T1,quarry,argus-quarry,Licence check,loop,8,recorded,2026-11-18,0.4.0,argus-quarry,16e992da7b,,"
    '"counts, or labels, change",week 1\n'
    "A1,atlas,argus-atlas,Atlas styles,clip,30,todo,,,argus-realms,,,,\n"
    "W1,,,Pipeline walkthrough,walkthrough,180,hold,,,,,,,\n"
)


def gif(path):
    frames = [Image.new("RGB", (8, 8), (i * 80, 0, 0)) for i in range(2)]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, comment=b"private note")
    return path


def cli(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text('{"pypi":{"projects":["demo"]},"destinations":{"discord":{"enabled":true}}}', encoding="utf-8")
    return ["--config", str(cfg), "--env", str(tmp_path / "no.env"), "--store", str(tmp_path / "s.sqlite")]


def test_add_set_validate_remove(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    clip = clips.add(store, "atlas-styles", demo="atlas", project="argus-atlas", title="Styles", target_seconds="30")
    assert (clip["status"], clip["format"], clip["target_seconds"], clip["recorded_at"]) == ("todo", "clip", 30, "")
    with pytest.raises(ReviewError, match="already exists"):
        clips.add(store, "atlas-styles")
    for bad_id in ("", "has space", "../x", "a" * 41):
        with pytest.raises(ReviewError, match="short slug"):
            clips.add(store, bad_id)
    for bad in (
        {"status": "done"},
        {"format": "tour"},
        {"target_seconds": "soon"},
        {"recorded_at": "yesterday"},
        {"demo_commit": "not-a-hash"},
        {"title": "x" * 501},
        {},
    ):
        with pytest.raises(ReviewError):
            clips.update(store, "atlas-styles", **bad)
    with pytest.raises(ReviewError, match="No clip"):
        clips.update(store, "nope", status="hold")

    # Becoming recorded stamps today; a given date wins; other moves leave the date alone.
    recorded = clips.update(store, "atlas-styles", status="recorded", demo_commit="16E992DA7B", project_version="0.4.0")
    assert len(recorded["recorded_at"]) == 10 and recorded["demo_commit"] == "16E992DA7B"
    clips.update(store, "atlas-styles", recorded_at="2026-01-02")
    assert clips.update(store, "atlas-styles", status="edited")["recorded_at"] == "2026-01-02"
    assert clips.update(store, "atlas-styles", status="stale")["recorded_at"] == "2026-01-02"
    assert (
        clips.update(store, "atlas-styles", status="recorded", recorded_at="2026-03-04")["recorded_at"] == "2026-03-04"
    )

    clips.remove(store, "atlas-styles")
    assert store.clips() == []
    store.close()


def test_import_hand_kept_list_then_round_trip(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    added, updated, ignored = clips.import_csv(store, "﻿" + HAND_KEPT)
    assert (added, updated, ignored) == (3, 0, ["notes"])
    t1 = store.clip("T1")
    assert (t1["project"], t1["target_seconds"], t1["demo_commit"]) == ("argus-quarry", 8, "16e992da7b")
    assert t1["stale_if"] == "counts, or labels, change" and store.clip("W1")["demo"] == ""

    exported = clips.export_csv(store)
    assert exported.splitlines()[0] == ",".join(clips.COLUMNS)
    other = Store(tmp_path / "other.sqlite")
    assert clips.import_csv(other, exported) == (3, 0, [])
    strip = lambda rows: [{k: v for k, v in r.items() if k not in ("created_at", "updated_at")} for r in rows]  # noqa: E731
    assert strip(other.clips()) == strip(store.clips())

    # A column in the file replaces what is stored; one that is absent is left alone.
    assert clips.import_csv(store, "id,status\nT1,posted\nNEW,scripted\n") == (1, 1, [])
    assert store.clip("T1")["status"] == "posted" and store.clip("T1")["title"] == "Licence check"
    store.close()
    other.close()


def test_import_checks_every_row_before_saving_any(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    with pytest.raises(ReviewError) as bad:
        clips.import_csv(store, "id,format,status\nok,loop,todo\nb1,tour,todo\nb2,clip,done\nok,clip,todo\n")
    message = str(bad.value)
    assert "line 3" in message and "line 4" in message and "line 5: clip ok appears twice" in message
    assert store.clips() == []
    for text in ("title\nx\n", "id,clip_id\na,b\n", "id\na,extra\n"):
        with pytest.raises(ReviewError):
            clips.import_csv(store, text)
    assert store.clips() == []
    store.close()


def test_coverage_names_demos_without_a_current_clip(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    clips.import_csv(store, HAND_KEPT)
    clips.add(store, "A2", demo="atlas", status="stale")
    clips.add(store, "Q2", demo="quarry", status="todo")
    assert clips.coverage(store.clips()) == (["quarry"], ["atlas"])
    report = clips.report(store)
    assert "Demos with a current clip: 1/2. None yet: atlas" in report
    clips.update(store, "A2", status="edited")
    assert clips.report(store).endswith("Demos with a current clip: 2/2")
    store.close()


def test_attaching_by_clip_records_the_link_and_cleans_the_file(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    ids = compose(store, [DiscordPublisher()], Post("demo", "Atlas", "Coastlines.", "https://example.org/atlas"))
    source = gif(tmp_path / "raw take FINAL.gif")
    clips.add(store, "atlas-loop", demo="atlas")
    with pytest.raises(ReviewError, match="no file yet"):
        clips.attach(store, tmp_path / "media", ids[0], "atlas-loop", "A coast")
    with pytest.raises(ReviewError, match="No clip"):
        clips.attach(store, tmp_path / "media", ids[0], "nope", "A coast")
    clips.update(store, "atlas-loop", path=str(source))

    media_ids = clips.attach(store, tmp_path / "media", ids[0], "atlas-loop", "A coast, painted")
    item = store.media_item(media_ids[0])
    assert (item["clip_id"], item["name"], item["alt"]) == ("atlas-loop", "atlas-loop.gif", "A coast, painted")
    assert item["path"] != str(source) and "comment" in item["removed"]
    assert b"private note" not in Path(item["path"]).read_bytes()

    bid = log_broadcast(store, platform="reddit", url="https://www.reddit.com/r/gis/comments/abc/", at="2026-12-01")
    clips.link_broadcast(store, bid, "atlas-loop")
    with pytest.raises(ReviewError):
        clips.link_broadcast(store, 999, "atlas-loop")
    uses = store.clip_uses("atlas-loop")
    assert [(u["via"], u["id"], u["platform"]) for u in uses] == [
        ("draft", ids[0], "discord"),
        ("logged", bid, "reddit"),
    ]
    assert "Used by 2 post(s):" in clips.describe(store, "atlas-loop")
    with pytest.raises(ReviewError, match="used by 2"):
        clips.remove(store, "atlas-loop")

    clips.update(store, "atlas-loop", path=str(tmp_path / "gone.gif"))
    with pytest.raises(ReviewError, match="No such file"):
        clips.attach(store, tmp_path / "media", ids[0], "atlas-loop", "A coast")
    store.close()


def test_store_from_before_the_registry_gains_the_columns(tmp_path):
    path = tmp_path / "s.sqlite"
    Store(path).close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE draft_media DROP COLUMN clip_id")
    conn.execute("ALTER TABLE logged_broadcasts DROP COLUMN clip_id")
    conn.execute("DROP TABLE clips")
    conn.commit()
    conn.close()
    store = Store(path)
    assert store.clips() == [] and store.clip_uses("x") == []
    store.close()


def test_cli(tmp_path, capsys):
    base = cli(tmp_path)
    assert main([*base, "clips"]) == 0
    assert "No clips yet" in capsys.readouterr().out
    source = gif(tmp_path / "loop.gif")
    assert main([*base, "clips", "add", "atlas-loop", "--demo", "atlas", "--title", "Loop", "--format", "loop"]) == 0
    assert main([*base, "clips", "add", "atlas-loop"]) == 2
    assert main([*base, "clips", "set", "atlas-loop"]) == 2
    assert main([*base, "clips", "set", "atlas-loop", "--status", "recorded", "--path", str(source)]) == 0
    assert "status=recorded" in capsys.readouterr().out
    assert main([*base, "clips"]) == 0
    assert "Demos with a current clip: 1/1" in capsys.readouterr().out

    assert main([*base, "compose", "Coastlines.", "--kind", "demo", "--subject", "Atlas"]) == 0
    assert main([*base, "media", "add", "1", "--clip", "atlas-loop"]) == 2  # alt text is still required
    assert main([*base, "media", "add", "1", "--clip", "nope", "--alt", "A coast"]) == 2
    capsys.readouterr()
    assert main([*base, "media", "add", "1", "--clip", "atlas-loop", "--alt", "A coast"]) == 0
    assert "Attached clip atlas-loop as gif to 1 draft(s)" in capsys.readouterr().out
    assert main([*base, "media", "list", "1"]) == 0
    assert "clip: atlas-loop" in capsys.readouterr().out

    logged = ["log-broadcast", "--platform", "hn", "--url", "https://news.ycombinator.com/item?id=1"]
    assert main([*base, *logged, "--clip", "nope"]) == 2
    assert main([*base, "broadcasts"]) == 0
    assert "No broadcasts logged" in capsys.readouterr().out  # an unknown clip logs nothing
    assert main([*base, *logged, "--clip", "atlas-loop"]) == 0
    assert "(clip atlas-loop)" in capsys.readouterr().out
    assert main([*base, "clips", "show", "atlas-loop"]) == 0
    shown = capsys.readouterr().out
    assert "draft #1" in shown and "logged #1" in shown

    out = tmp_path / "clips.csv"
    assert main([*base, "clips", "export", str(out)]) == 0
    assert main([*base, "clips", "export"]) == 0
    assert out.read_text(encoding="utf-8") in capsys.readouterr().out
    assert main([*base, "clips", "import", str(out)]) == 0
    assert "Imported 1 clips (0 new, 1 updated)." in capsys.readouterr().out
    assert main([*base, "clips", "import", str(tmp_path / "missing.csv")]) == 2
    assert main([*base, "clips", "remove", "atlas-loop"]) == 2
