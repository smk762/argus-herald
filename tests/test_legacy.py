"""The pip-release names keep working, with a warning, until 0.3."""

import importlib
import logging
import warnings

import argus_herald
import argus_herald.platforms as platforms
from argus_herald import legacy, privacy
from argus_herald.store import Store


def test_legacy_env_is_adopted_but_never_overrides(caplog):
    legacy._warned.clear()
    env = {"PIP_RELEASE_UI_TOKEN": "old", "PIP_RELEASE_STORE": "/x", "ARGUS_HERALD_STORE": "/new"}
    with caplog.at_level(logging.WARNING, logger="argus_herald"):
        assert legacy.adopt_legacy_env(env) == ["PIP_RELEASE_UI_TOKEN"]
    assert env["ARGUS_HERALD_UI_TOKEN"] == "old" and env["ARGUS_HERALD_STORE"] == "/new"
    assert "rename it to ARGUS_HERALD_UI_TOKEN" in caplog.text


def test_data_dir_moves_once(tmp_path):
    old = tmp_path / ".pip-release"
    old.mkdir()
    Store(old / "state.sqlite").close()
    (old / "metrics.sqlite").write_bytes(b"")
    new_store = tmp_path / ".argus-herald" / "state.sqlite"
    store = Store(new_store)
    try:
        assert store.path == new_store and new_store.is_file() and (new_store.parent / "metrics.sqlite").is_file()
    finally:
        store.close()
    assert not old.exists()
    assert not legacy.migrate_data_dir(new_store)  # already moved


def test_explicit_store_paths_stay_put(tmp_path):
    old = tmp_path / ".pip-release"
    old.mkdir()
    (old / "state.sqlite").write_bytes(b"")
    assert not legacy.migrate_data_dir(tmp_path / "elsewhere" / "state.sqlite")
    (tmp_path / ".argus-herald").mkdir()
    assert not legacy.migrate_data_dir(tmp_path / ".argus-herald" / "state.sqlite")  # new dir already in use
    assert (old / "state.sqlite").is_file()


def test_old_denylist_place_is_read_with_a_warning(tmp_path, monkeypatch):
    old = tmp_path / "old-denylist.txt"
    old.write_text("zz-legacy-term\n", encoding="utf-8")
    old.chmod(0o600)
    monkeypatch.setattr(privacy, "DEFAULT_DENYLIST", tmp_path / "missing.txt")
    monkeypatch.setattr(privacy, "LEGACY_DENYLIST", old)
    gate = privacy.load_gate({}, {"AI_AUDIT_OPSEC_DENYLIST": str(tmp_path / "none.txt")})
    assert "zz-legacy-term" in gate.deny_terms
    assert any("old place" in w for w in gate.warnings)


def test_old_import_name_is_the_same_module():
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        old = importlib.import_module("pip_release")
        old_platforms = importlib.import_module("pip_release.platforms")
    assert old_platforms is platforms and old.__version__ == argus_herald.__version__
    assert old_platforms.Publisher is platforms.Publisher


def test_old_entry_point_group_still_loads(monkeypatch, caplog):
    legacy._warned.clear()

    class Plugin:
        id = "example"

    class Entry:
        name = "example"

        def load(self):
            return Plugin

    monkeypatch.setattr(
        platforms, "entry_points", lambda group: [Entry()] if group == legacy.LEGACY_PLATFORM_GROUP else []
    )
    with caplog.at_level(logging.WARNING, logger="argus_herald"):
        found = {p.id for p in platforms.load_publishers()}
    assert "example" in found and "argus_herald.platforms" in caplog.text
