from pathlib import Path

from argus_herald.cli import main, parse_edit


def test_parse_edit_reads_title_and_body():
    title, body = parse_edit("title: Atlas 1.2\n\nHello there\n", "old")
    assert title == "Atlas 1.2"
    assert body == "Hello there"


def test_parse_edit_without_title_keeps_the_previous_one():
    title, body = parse_edit("just the post", "kept")
    assert title == "kept"
    assert body == "just the post"


def test_platforms_command_lists_the_dragonhound_accounts(tmp_path, capsys):
    code = main(
        [
            "--config",
            str(Path(__file__).resolve().parents[1] / "examples" / "dragonhound.yaml"),
            "--env",
            str(tmp_path / "does-not-exist.env"),
            "--store",
            str(tmp_path / "state.sqlite"),
            "platforms",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "dragonhound3" in out
    assert "AtlasRealms" in out
    assert "manual" in out
    assert "missing X_API_KEY" not in out
    assert "disabled" in out


def test_drafts_on_an_empty_store(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(
        '{"pypi":{"projects":["demo"]},"destinations":{"patreon":{"enabled":true}}}',
        encoding="utf-8",
    )
    code = main(
        ["--config", str(config), "--env", str(tmp_path / "no.env"), "--store", str(tmp_path / "s.sqlite"), "drafts"]
    )
    assert code == 0
    assert "No drafts." in capsys.readouterr().out


def test_init_copies_the_dragonhound_example(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("examples").mkdir()
    Path("examples/dragonhound.yaml").write_text("pypi: {projects: [demo]}\n", encoding="utf-8")
    Path(".env.example").write_text("X_API_KEY=\n", encoding="utf-8")
    code = main(["init", "--example", "dragonhound"])
    assert code == 0
    assert Path("config.yaml").read_text(encoding="utf-8").startswith("pypi:")
    assert Path(".env").is_file()


def _seeded(tmp_path):
    from argus_herald.store import Store

    config = tmp_path / "config.json"
    config.write_text(
        '{"pypi":{"projects":["demo"]},"destinations":{"patreon":{"enabled":true}}}',
        encoding="utf-8",
    )
    path = tmp_path / "s.sqlite"
    store = Store(path)
    for version in ("1", "2"):
        store.add_draft(project="demo", version=version, platform="patreon", title="t", body="b", link="l")
    store.conn.execute("UPDATE drafts SET created_at = '2026-10-01T00:00:00+00:00' WHERE version = '1'")
    store.close()
    base = ["--config", str(config), "--env", str(tmp_path / "no.env"), "--store", str(path)]
    return base, path


def _statuses(path):
    from argus_herald.store import Store

    store = Store(path)
    try:
        return {d.version: (d.status, d.note) for d in store.list_drafts()}
    finally:
        store.close()


def test_reset_rejects_pending_before_a_date(tmp_path, capsys):
    base, path = _seeded(tmp_path)
    assert main([*base, "reset", "--reject-pending", "--before", "2026-11-01", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "1 pending drafts created before 2026-11-01" in out
    assert _statuses(path) == {"1": ("rejected", "stale backlog"), "2": ("pending", None)}


def test_reset_asks_first_and_keeps_drafts_on_no(tmp_path, monkeypatch, capsys):
    import argus_herald.cli as cli

    base, path = _seeded(tmp_path)
    monkeypatch.setattr(cli, "_confirm", lambda prompt: False)
    assert main([*base, "reset", "--reject-pending"]) == 1
    assert "Nothing changed." in capsys.readouterr().out
    assert {s for s, _ in _statuses(path).values()} == {"pending"}


def test_reset_without_a_terminal_needs_yes(tmp_path, capsys):
    base, path = _seeded(tmp_path)
    assert main([*base, "reset", "--reject-pending"]) == 2
    assert "Pass --yes" in capsys.readouterr().err
    assert main([*base, "reset"]) == 2


def test_parse_cutoff_normalises_to_utc():
    from argus_herald.cli import parse_cutoff

    assert parse_cutoff("2026-11-15") == "2026-11-15"
    assert parse_cutoff("2026-11-15T05:00:00-05:00") == "2026-11-15T10:00:00+00:00"
    assert parse_cutoff("2026-11-15T10:00:00Z") == "2026-11-15T10:00:00+00:00"


def test_check_and_accept_and_fail_closed(tmp_path, capsys, monkeypatch):
    from argus_herald.store import Store

    base, path = _seeded(tmp_path)
    store = Store(path)
    store.update_text(1, "t", "reach me at bob@corp.test", ("pending",))
    store.close()

    assert main([*base, "check"]) == 1
    out = capsys.readouterr().out
    assert "email address" in out and "argus-herald accept 1 'email:bob@corp.test'" in out
    assert main([*base, "approve", "1"]) == 1
    assert "Refused by the privacy gate" in capsys.readouterr().err

    assert main([*base, "accept", "1", "email:bob@corp.test", "--note", "public address"]) == 0
    assert main([*base, "check", "1"]) == 0
    assert main([*base, "approve", "1"]) == 0

    monkeypatch.setenv("ARGUS_HERALD_OPSEC_DENYLIST", str(tmp_path / "gone.txt"))
    assert main([*base, "approve", "2"]) == 2
    assert "deny-list is missing" in capsys.readouterr().err


def test_compose_reads_stdin_and_drafts_each_destination(tmp_path, capsys, monkeypatch):
    import io

    base, path = _seeded(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("A build note about provenance.\n"))
    code = main([*base, "compose", "-", "--kind", "note", "--link", "https://example.org/n", "--subject", "Provenance"])
    assert code == 0
    assert "drafted #3 patreon" in capsys.readouterr().out
    assert main([*base, "drafts", "--status", "pending"]) == 0
    assert "note     Provenance" in capsys.readouterr().out
    assert main([*base, "compose", "x", "--platform", "twitter"]) == 2


def test_media_commands_and_compose_with_media(tmp_path, capsys):
    from PIL import Image

    base, path = _seeded(tmp_path)
    Image.new("RGB", (8, 8)).save(tmp_path / "shot.png")
    assert main([*base, "media", "add", "1", str(tmp_path / "shot.png")]) == 2  # alt required
    assert main([*base, "media", "add", "1", str(tmp_path / "shot.png"), "--alt", "A black square"]) == 0
    assert "Attached shot.png as image to 1 draft(s)" in capsys.readouterr().out
    assert main([*base, "media", "list", "1"]) == 0
    assert "alt: A black square" in capsys.readouterr().out
    assert main([*base, "media", "alt", "1", "A dark square"]) == 0
    assert main([*base, "show", "1"]) == 0 and "alt: A dark square" in capsys.readouterr().out
    assert main([*base, "media", "remove", "1"]) == 0
    assert main([*base, "compose", "Clip", "--media", str(tmp_path / "shot.png")]) == 2  # one --alt per --media
    assert main([*base, "compose", "Clip", "--media", str(tmp_path / "shot.png"), "--alt", "Square"]) == 0
    from argus_herald.store import Store

    store = Store(path)
    assert [m["alt"] for d in store.list_drafts() if d.kind == "demo" for m in store.media(d.id)] == ["Square"]
    store.close()


def _edit_setup(tmp_path, monkeypatch, editor):
    from argus_herald.store import Store

    config = tmp_path / "config.json"
    config.write_text('{"pypi":{"projects":["demo"]},"destinations":{"patreon":{"enabled":true}}}', encoding="utf-8")
    store = Store(tmp_path / "s.sqlite")
    store.add_draft(project="demo", version="1.0", platform="patreon", title="demo 1.0", body="Old body", link="")
    store.close()
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", editor)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)  # read TMPDIR again
    return ["--config", str(config), "--env", str(tmp_path / "no.env"), "--store", str(tmp_path / "s.sqlite")]


def test_edit_runs_an_editor_command_with_arguments_on_a_private_file(tmp_path, monkeypatch, capsys):
    import sys

    from argus_herald.store import Store

    script = tmp_path / "editor.py"
    script.write_text(
        "import os, stat, sys\n"
        "flag, path = sys.argv[1], sys.argv[2]\n"
        "mode = stat.S_IMODE(os.stat(path).st_mode)\n"
        f"open({str(tmp_path / 'seen.txt')!r}, 'w').write(f'{{flag}} {{path}} {{mode:o}}')\n"
        "open(path, 'w').write('title: demo 1.0\\n\\nNew body\\n')\n",
        encoding="utf-8",
    )
    base = _edit_setup(tmp_path, monkeypatch, f"{sys.executable} {script} --wait")
    assert main([*base, "edit", "1"]) == 0
    flag, path, mode = (tmp_path / "seen.txt").read_text().split(" ")
    assert flag == "--wait" and mode == "600"
    assert Path(path).name != "argus-herald-1.txt" and Path(path).name.startswith("argus-herald-1-")
    assert not Path(path).exists() and list((tmp_path / "tmp").iterdir()) == []  # removed when done
    store = Store(tmp_path / "s.sqlite")
    assert store.get(1).body == "New body"
    store.close()


def test_edit_never_writes_through_a_file_planted_at_a_guessable_path(tmp_path, monkeypatch):
    import sys

    base = _edit_setup(tmp_path, monkeypatch, f"{sys.executable} -c pass")
    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    (tmp_path / "tmp" / "argus-herald-1.txt").symlink_to(victim)
    assert main([*base, "edit", "1"]) == 0
    assert victim.read_text(encoding="utf-8") == "precious"


def test_edit_with_a_missing_editor_is_an_error_not_a_traceback(tmp_path, monkeypatch, capsys):
    from argus_herald.store import Store

    base = _edit_setup(tmp_path, monkeypatch, "no-such-editor-zz --wait")
    assert main([*base, "edit", "1"]) == 2
    assert "Can't run the editor" in capsys.readouterr().err
    assert list((tmp_path / "tmp").iterdir()) == []
    store = Store(tmp_path / "s.sqlite")
    assert store.get(1).body == "Old body"
    store.close()


def test_compose_checks_every_media_file_before_it_drafts(tmp_path, capsys):
    from PIL import Image

    base, path = _seeded(tmp_path)
    good = tmp_path / "ok.png"
    Image.new("RGB", (8, 8)).save(good)
    wrong = ["--media", str(good), "--alt", "a square", "--media", str(tmp_path / "missing.png"), "--alt", "gone"]
    assert main([*base, "compose", "A build note", *wrong]) == 2
    assert "No such file" in capsys.readouterr().err
    assert set(_statuses(path)) == {"1", "2"}  # no image-less draft to approve by mistake, or to duplicate
    assert main([*base, "compose", "A build note", "--media", str(good), "--alt", "a square"]) == 0
    assert len(_statuses(path)) == 3
