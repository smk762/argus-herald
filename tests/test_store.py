from argus_herald.store import Store


def _add(store, source, **fields):
    base = {
        "project": "argus-atlas",
        "version": "0.4.0",
        "released_at": "2026-11-18T00:00:00+00:00",
        "summary": "",
        "notes": "",
        "url": "",
    }
    return store.add_release(source=source, **{**base, **fields})


def test_a_pypi_release_keeps_what_was_set_on_its_github_twin(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    assert _add(store, "github", notes="### Added\n- From GitHub\n", summary="v0.4.0")
    twin = store.release("argus-atlas", "0.4.0", source="github")
    store.set_excluded(twin["id"], True)
    _add(store, "pypi", summary="Bake tiles", url="https://pypi.org/project/argus-atlas/0.4.0/")
    assert store.release("argus-atlas", "0.4.0", source="github") is None
    merged = store.release("argus-atlas", "0.4.0")
    assert merged["excluded"] == 1  # the maintainer's exclusion survives the merge
    assert (merged["id"], merged["summary"]) == (twin["id"], "Bake tiles")
    assert "From GitHub" in merged["notes"] and merged["url"].startswith("https://pypi.org/")
    assert len(store.releases()) == 1
    store.close()


def test_a_merge_that_meets_an_existing_pypi_row_loses_nothing(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    _add(store, "github", notes="GitHub notes")
    # Another poller recorded the PyPI release in between.
    store.conn.execute(
        "INSERT INTO releases (source, project, version, released_at, seen_at) "
        "VALUES ('pypi', 'argus-atlas', '0.4.0', '2026-11-18', '2026-11-18')"
    )
    twin = store.conn.execute("SELECT * FROM releases WHERE source = 'github'").fetchone()
    # What add_release does once it has read the twin: the GitHub row and its notes must survive.
    store.release = lambda project, version, source="pypi": twin if source == "github" else None
    assert _add(store, "pypi") is False
    kept = {row["source"]: row["notes"] for row in store.conn.execute("SELECT * FROM releases")}
    assert kept == {"github": "GitHub notes", "pypi": ""}
    store.close()


def test_add_draft_raises_when_a_row_is_refused_for_anything_but_an_existing_draft(tmp_path):
    import sqlite3

    import pytest

    store = Store(tmp_path / "s.sqlite")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_draft(project="argus-atlas", version="0.1.0", platform="twitter", title=None, body="b", link="l")
    assert store.add_draft(project="argus-atlas", version="0.1.0", platform="twitter", title="", body="b", link="l")[1]
    assert not store.add_draft(
        project="argus-atlas", version="0.1.0", platform="twitter", title="", body="b", link="l"
    )[1]
    store.close()


def test_transition_and_mint_reports_roll_back_with_an_enclosing_transaction(tmp_path):
    import pytest

    store = Store(tmp_path / "s.sqlite")
    draft_id, _ = store.add_draft(
        project="argus-atlas", version="0.1.0", platform="twitter", title="", body="b", link="l"
    )
    with pytest.raises(RuntimeError), store.transaction():
        assert store.transition(draft_id, ("pending",), "approved") is not None
        store.add_mint_report(draft_id, mint_id="abc", platform="twitter", url=None, at="2026-11-18", sha256="0" * 64)
        store.settle_mint_report(draft_id, reported=True)
        raise RuntimeError("a later step fails")
    assert store.get(draft_id).status == "pending"
    assert store.mint_report(draft_id) is None
    # Outside a transaction each one still stands on its own.
    assert store.transition(draft_id, ("pending",), "approved") is not None
    store.close()
    assert Store(tmp_path / "s.sqlite").get(draft_id).status == "approved"
