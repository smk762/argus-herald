from pathlib import Path

from pip_release.cli import main, parse_edit


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
            "examples/dragonhound.yaml",
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
    assert "missing X_API_KEY" in out
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
    from pip_release.store import Store

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
    from pip_release.store import Store

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
    import pip_release.cli as cli

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
    from pip_release.cli import parse_cutoff

    assert parse_cutoff("2026-11-15") == "2026-11-15"
    assert parse_cutoff("2026-11-15T05:00:00-05:00") == "2026-11-15T10:00:00+00:00"
    assert parse_cutoff("2026-11-15T10:00:00Z") == "2026-11-15T10:00:00+00:00"
