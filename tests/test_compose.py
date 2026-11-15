import sqlite3
from pathlib import Path

import pytest

from pip_release.config import ConfigError, load_config
from pip_release.feeds import parse_atom
from pip_release.models import Post
from pip_release.platforms import PatreonPublisher, TwitterPublisher
from pip_release.store import Store
from pip_release.workflow import ReviewError, compose, poll

FIXTURES = Path(__file__).parent / "fixtures"
SITE = "https://example.org/updates.xml"
CHANNEL = "UC" + "a" * 22
YT = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}"


class FeedHttp:
    def __init__(self, feeds):
        self.feeds = feeds

    def request(self, method, url, body=None, headers=None):
        return (200, self.feeds[url]) if url in self.feeds else (404, b"")


def _config(tmp_path, sources):
    path = tmp_path / "config.yaml"
    path.write_text(
        "destinations:\n  twitter: {enabled: true}\n  patreon: {enabled: true}\n" + sources,
        encoding="utf-8",
    )
    cfg = load_config(path)
    cfg.store_path = tmp_path / "state.sqlite"
    return cfg


def _feed(name):
    return (FIXTURES / name).read_bytes()


def test_parse_atom_reads_site_and_youtube_and_refuses_dtds():
    site = parse_atom(_feed("site_updates.xml"))
    assert [(i.title, i.categories) for i in site] == [("Digest: first one", ("digest",)), ("Curator's numbers", ("note",))]
    video = parse_atom(_feed("youtube.xml"))[0]
    assert video.link == "https://www.youtube.com/watch?v=AAAAAAAAAAA" and video.summary == "A short demo."
    with pytest.raises(ValueError, match="DTD"):
        parse_atom(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><feed xmlns="http://www.w3.org/2005/Atom"/>')


def test_sources_config_is_validated(tmp_path):
    cfg = _config(tmp_path, f"sources:\n  site: {{feed: {SITE}}}\n  youtube: {{channel_id: {CHANNEL}, platforms: [twitter]}}\n")
    assert [(f.name, f.url, f.categories, f.platforms) for f in cfg.feeds] == [
        ("site", SITE, ("note",), ()),
        ("youtube", YT, (), ("twitter",)),
    ]
    for bad in ("sources:\n  youtube: {channel_id: nope}\n", "sources:\n  site: {feed: http://x.org/f}\n",
                "sources:\n  rss: {}\n", f"sources:\n  youtube: {{channel_id: {CHANNEL}, platforms: [discord]}}\n"):
        with pytest.raises(ConfigError):
            _config(tmp_path, bad)


def test_feeds_baseline_then_draft_new_items(tmp_path):
    cfg = _config(tmp_path, f"sources:\n  site: {{feed: {SITE}}}\n  youtube: {{channel_id: {CHANNEL}, platforms: [twitter]}}\n")
    store = Store(cfg.store_path)
    pubs = [TwitterPublisher(), PatreonPublisher()]
    empty_yt = _feed("youtube.xml").split(b"<entry>")[0] + b"</feed>"
    site_old = _feed("site_updates.xml").split(b"  <entry>\n    <id>tag:example.org,2026:updates/2026-11-20")[0] + b"</feed>"
    http = FeedHttp({SITE: site_old, YT: empty_yt})

    first = poll(cfg, store, http, pubs)
    assert [(e.kind, e.project) for e in first] == [("baseline", "site"), ("baseline", "youtube")]
    assert store.list_drafts() == []

    http.feeds = {SITE: _feed("site_updates.xml"), YT: _feed("youtube.xml")}
    events = poll(cfg, store, http, pubs)
    assert [(e.kind, e.project, e.version) for e in events] == [
        ("draft", "site", "Curator's numbers"),
        ("draft", "youtube", "Atlas: painting a coastline"),
    ]
    drafts = store.list_drafts()
    assert [(d.kind, d.platform, d.label) for d in drafts] == [
        ("note", "twitter", "Curator's numbers"),
        ("note", "patreon", "Curator's numbers"),
        ("video", "twitter", "Atlas: painting a coastline"),
    ]
    assert drafts[0].body == (
        "Curator's numbers\n\nHow the curator scores a dataset.\n\n"
        "https://example.org/updates/2026-11-20-curator-numbers/"
    )
    assert drafts[0].post_key == "site:tag:example.org,2026:updates/2026-11-20-curator-numbers"
    assert drafts[2].body.startswith("New video: Atlas: painting a coastline\n\nhttps://www.youtube.com/watch")
    assert poll(cfg, store, http, pubs) == []
    store.close()


def test_compose_makes_one_draft_per_destination(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    post = Post(kind="demo", subject="", text="Hex territories on real terrain.\nShort clip.", link="https://example.org/realms", title="Realms demo")
    ids = compose(store, [TwitterPublisher(), PatreonPublisher()], post)
    x, patreon = (store.get(i) for i in ids)
    assert x.body == "Hex territories on real terrain.\nShort clip.\n\nhttps://example.org/realms"
    assert (x.title, patreon.title) == ("", "Realms demo")
    assert x.post_key == patreon.post_key and x.post_key.startswith("post:")
    assert x.label == "Realms demo"  # no label given: the title, then the first line of text
    untitled = compose(store, [TwitterPublisher()], Post(kind="demo", subject="", text="First line\nmore", link=""))
    assert store.get(untitled[0]).label == "First line"
    long = compose(store, [TwitterPublisher()], Post(kind="note", subject="Long", text="word " * 100, link="https://example.org/n"))
    assert store.get(long[0]).body.endswith("https://example.org/n")
    for bad in (Post("demo", "", "", ""), Post("demo", "", "x", "http://example.org"), Post("release", "", "x", "")):
        with pytest.raises(ReviewError):
            compose(store, [TwitterPublisher()], bad)
    store.close()


def test_old_drafts_table_is_migrated_with_accepts_intact(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, version TEXT NOT NULL,
            platform TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, link TEXT NOT NULL,
            status TEXT NOT NULL, edited INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, note TEXT, external_id TEXT, error TEXT,
            UNIQUE (project, version, platform)
        );
        CREATE TABLE gate_accepts (
            draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE, key TEXT NOT NULL,
            note TEXT NOT NULL, accepted_at TEXT NOT NULL, PRIMARY KEY (draft_id, key)
        );
        INSERT INTO drafts VALUES (7, 'argus-atlas', '0.2.0', 'twitter', '', 'b', 'l', 'pending', 0, 't', 't', NULL, NULL, NULL);
        INSERT INTO gate_accepts VALUES (7, 'email:a@b.test', 'fine', 't');
        """
    )
    conn.commit()
    conn.close()
    store = Store(path)
    draft = store.get(7)
    assert (draft.kind, draft.post_key, draft.label) == ("release", "release:argus-atlas==0.2.0", "argus-atlas 0.2.0")
    assert store.accepted(7) == {"email:a@b.test": "fine"}
    assert store.add_draft(project="argus-atlas", version="0.2.0", platform="twitter", title="", body="b", link="l") == (7, False)
    store.close()
