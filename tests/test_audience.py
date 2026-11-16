import pytest

from argus_herald.config import Audience, ConfigError, Destination, load_config
from argus_herald.digest import build_digest, promote
from argus_herald.models import Release, Rendered
from argus_herald.platforms import TwitterPublisher, configured_publishers
from argus_herald.store import Store
from argus_herald.workflow import _drafts_for, confirm


def cfg_text(tmp_path, destinations, extra=""):
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    path = tmp_path / "c.yaml"
    path.write_text(
        "pypi: {projects: [argus-atlas]}\ndestinations:\n"
        + destinations
        + f"digest: {{anchor: 2026-11-16, site_url: https://example.org/updates/, site_dir: {site}}}\n"
        + extra,
        encoding="utf-8",
    )
    cfg = load_config(path)
    cfg.store_path = tmp_path / "s.sqlite"
    return cfg


DESTS = """  twitter:
    enabled: true
    audience:
      description: Crypto and AI-image people
      tone: plain, a little dry
      hashtags: ["#gis", OpenData]
      link_placement: reply
  patreon:
    enabled: true
    audience:
      packages: [argus-atlas, argus-realms]
"""


def test_config_and_globs(tmp_path):
    cfg = cfg_text(tmp_path, DESTS)
    tw, pa = cfg.destinations["twitter"].audience, cfg.destinations["patreon"].audience
    assert tw.hashtags == ("gis", "OpenData") and tw.link_placement == "reply"
    assert pa.wants("argus-atlas") and not pa.wants("argus-lens")
    assert Audience(include=("argus-*",), exclude=("argus-ci",)).wants("Argus-Lens")
    assert not Audience(include=("argus-*",), exclude=("argus-ci",)).wants("argus-ci")
    for bad in (
        "  patreon: {audience: {link_placement: reply}}\n",
        "  twitter: {mode: api, audience: {link_placement: reply}}\n",
        "  twitter: {audience: {hashtags: ['two words']}}\n",
        "  twitter: {audience: {link_placement: sideways}}\n",
    ):
        with pytest.raises(ConfigError):
            cfg_text(tmp_path, bad)


def test_shape_moves_link_and_adds_hashtags_that_fit():
    x = TwitterPublisher().configure(
        Destination(audience=Audience(hashtags=("gis", "OpenData", "third"), link_placement="reply"))
    )
    rendered, reply = x.shape(Rendered("", "Coastlines, painted.\n\nhttps://example.org/a"), "https://example.org/a")
    assert reply == "https://example.org/a"
    assert rendered.body == "Coastlines, painted.\n\n#gis #OpenData"  # X takes two
    full, _ = x.shape(Rendered("", "y" * 275), "")
    assert full.body == "y" * 275  # no room, no hashtags
    plain = TwitterPublisher()
    assert plain.shape(Rendered("", "a https://e.org"), "https://e.org") == (Rendered("", "a https://e.org"), "")


def _digest(store, packages):
    for i, project in enumerate(packages):
        store.add_release(
            project=project,
            version=f"0.{i + 2}.0",
            released_at=f"2026-11-1{8 + i}T00:00:00+00:00",
            summary="Bake tiles",
            notes="### Added\n- Painted coastlines\n",
            url=f"https://pypi.org/project/{project}/",
        )
    return store.create_digest("2026-11-16", "2026-11-29", "2026-11-30T00:00:00+00:00")


def test_digest_only_reaches_destinations_that_want_its_packages(tmp_path, gate):
    cfg = cfg_text(tmp_path, DESTS)
    pubs = [p for p in configured_publishers(cfg).values() if p.id in ("twitter", "patreon")]
    store = Store(cfg.store_path)
    digest_id = _digest(store, ["argus-atlas"])
    ids, _status = build_digest(store, cfg.digest, pubs, digest_id, gate)
    drafts = {store.get(i).platform: store.get(i) for i in ids}
    assert set(drafts) == {"twitter", "patreon"}
    assert "https://example.org/updates/2026-11-30-digest/" not in drafts["twitter"].body
    assert drafts["twitter"].reply == "https://example.org/updates/2026-11-30-digest/"
    assert drafts["twitter"].body.endswith("#gis #OpenData")
    assert (
        drafts["patreon"].body.endswith("https://example.org/updates/2026-11-30-digest/")
        and not drafts["patreon"].reply
    )
    store.close()

    second = tmp_path / "second"
    second.mkdir()
    cfg2 = cfg_text(second, DESTS)
    store = Store(cfg2.store_path)
    digest_id = _digest(store, ["argus-lens", "argus-cortex"])
    ids, _ = build_digest(store, cfg2.digest, pubs, digest_id, gate)
    assert [store.get(i).platform for i in ids] == ["twitter"]  # nothing for Patreon's audience
    store.close()


def test_mixed_digest_gives_patreon_only_its_packages(tmp_path, gate):
    cfg = cfg_text(tmp_path, DESTS)
    pubs = [p for p in configured_publishers(cfg).values() if p.id in ("twitter", "patreon")]
    store = Store(cfg.store_path)
    digest_id = _digest(store, ["argus-atlas", "argus-lens"])
    ids, _ = build_digest(store, cfg.digest, pubs, digest_id, gate)
    bodies = {store.get(i).platform: store.get(i).body for i in ids}
    assert "2 releases across 2 packages" in bodies["twitter"]
    assert "1 release across 1 package" in bodies["patreon"] and "lens" not in bodies["patreon"]
    store.close()


def test_per_release_and_promote_respect_the_filter_and_x_gets_a_reply_step(tmp_path, gate):
    cfg = cfg_text(tmp_path, DESTS)
    pubs = [p for p in configured_publishers(cfg).values() if p.id in ("twitter", "patreon")]
    store = Store(cfg.store_path)
    lens = Release(
        project="argus-lens",
        version="0.13.0",
        summary="Index",
        url="https://pypi.org/project/argus-lens/0.13.0/",
        author=None,
        project_urls={},
        yanked=False,
    )
    assert [store.get(i).platform for i in _drafts_for(store, pubs, lens)] == ["twitter"]
    _digest(store, ["argus-lens"])
    ids, _ = promote(store, cfg.digest, pubs, store.release("argus-lens", "0.2.0"))
    assert [store.get(i).platform for i in ids] == ["twitter"]

    x = store.get(ids[0])
    store.conn.execute("UPDATE drafts SET status = 'handed_off' WHERE id = ?", (x.id,))
    confirmed = confirm(store, x.id, "https://x.com/dragonhound3/status/123")
    link = TwitterPublisher().reply_url(confirmed)
    assert link.startswith("https://x.com/intent/post?in_reply_to=123&text=https%3A%2F%2Fpypi.org")
    store.close()
