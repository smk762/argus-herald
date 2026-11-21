import json
import sqlite3
from pathlib import Path

import pytest

from argus_herald.config import ConfigError, load_config
from argus_herald.github import html_to_text, list_releases, version_from_tag
from argus_herald.platforms import PatreonPublisher, TwitterPublisher
from argus_herald.store import Store
from argus_herald.workflow import poll

FEED = (Path(__file__).parent / "fixtures" / "gh_releases.atom").read_bytes()


def rest(*items):
    return json.dumps(
        [
            {
                "tag_name": t,
                "name": n,
                "body": b,
                "html_url": f"https://github.com/smk762/argus-studio/releases/tag/{t}",
                "published_at": when,
                "draft": d,
                "prerelease": p,
            }
            for t, n, b, when, d, p in items
        ]
    ).encode()


class GH:
    def __init__(self, releases=None, feed=FEED, pypi=None):
        self.releases, self.feed, self.pypi = releases or {}, feed, pypi or {}
        self.calls = []

    def request(self, method, url, body=None, headers=None):
        self.calls.append(url)
        if url.startswith("https://api.github.com/repos/smk762/") and "/releases" in url:
            repo = url.split("/")[5]
            return (200, self.releases[repo]) if repo in self.releases else (404, b"[]")
        if url.endswith("/releases.atom"):
            return 200, self.feed
        if url.startswith("https://pypi.org/pypi/"):
            name = url.split("/")[4]
            if name in self.pypi:
                versions = self.pypi[name]
                data = {
                    "info": {"name": name, "version": list(versions)[-1], "summary": "s", "project_urls": {}},
                    "releases": {v: [{"yanked": False, "upload_time_iso_8601": t}] for v, t in versions.items()},
                }
                return 200, json.dumps(data).encode()
        return 404, b"{}"


def test_tags_and_html():
    assert [version_from_tag(t) for t in ("v0.4.0", "0.3.0rc1", "v1.2", "stock-heads", "release-2026")] == [
        "0.4.0",
        "0.3.0rc1",
        "1.2",
        None,
        None,
    ]
    text = html_to_text("<p>Intro <code>x</code>.</p><h3>Added</h3><ul><li><p>One (#5).</p></li><li>Two</li></ul>")
    assert text == "Intro `x`.\n\n### Added\n\n- One (#5).\n- Two"


def test_feed_without_token_and_api_with_token():
    feed = list_releases(GH(), "smk762", "argus-cull")
    assert [(r.version, r.prerelease) for r in feed] == [("0.1.0", False), ("0.4.0", False)]
    assert (
        feed[1].notes.startswith("First public release")
        and "- Export into a curated output root (#50)." in feed[1].notes
    )
    http = GH(
        releases={
            "argus-studio": rest(
                ("v0.5.0", "Studio 0.5", "### Added\n- Jobs view", "2026-11-20T00:00:00Z", False, False),
                ("v0.6.0", "draft", "", "2026-11-21T00:00:00Z", True, False),
                ("v0.6.0rc1", "rc", "", "2026-11-21T00:00:00Z", False, True),
            )
        }
    )
    api = list_releases(http, "smk762", "argus-studio", "tok")
    assert [(r.version, r.prerelease) for r in api] == [("0.5.0", False), ("0.6.0rc1", True)]
    assert not any(u.endswith(".atom") for u in http.calls)


def test_a_percent_encoded_tag_in_the_feed_is_decoded_like_the_api_tag_name():
    feed = (
        b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>r1</id><title>rc</title>'
        b'<link rel="alternate" href="https://github.com/smk762/argus-cull/releases/tag/v0.5.0-rc%2E1"/>'
        b"<updated>2026-11-20T00:00:00Z</updated></entry></feed>"
    )
    [release] = list_releases(GH(feed=feed), "smk762", "argus-cull")
    assert (release.tag, release.version) == ("v0.5.0-rc.1", "0.5.0-rc.1")


def _config(tmp_path, digest=True, extra=""):
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    text = (
        "pypi: {projects: [argus-lens]}\n"
        "github: {owner: smk762, repos: [argus-studio, argus-lens]" + extra + "}\n"
        "destinations:\n  twitter: {enabled: true}\n  patreon: {enabled: true}\n"
    )
    if digest:
        text += f"digest: {{anchor: 2026-11-16, site_url: https://example.org/updates/, site_dir: {site}}}\n"
    path = tmp_path / "c.yaml"
    path.write_text(text, encoding="utf-8")
    cfg = load_config(path)
    cfg.store_path = tmp_path / "s.sqlite"
    return cfg


PUBS = [TwitterPublisher(), PatreonPublisher()]


def test_digest_mode_collects_github_and_dedupes_with_pypi(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    cfg = _config(tmp_path)
    store = Store(cfg.store_path)
    old = ("v0.4.0", "Studio 0.4", "", "2026-11-01T00:00:00Z", False, False)
    http = GH(
        releases={
            "argus-studio": rest(old),
            "argus-lens": rest(("v0.12.0", "", "", "2026-11-01T00:00:00Z", False, False)),
        },
        pypi={"argus-lens": {"0.12.0": "2026-11-01T00:00:00Z"}},
    )
    first = poll(cfg, store, http, PUBS)
    assert sorted(e.project for e in first if e.kind == "baseline") == [
        "argus-lens",
        "argus-lens (github)",
        "argus-studio (github)",
    ]

    # lens 0.13.0 shows up on GitHub first, then on PyPI: one release, under pypi, with GitHub's notes
    http.releases["argus-lens"] = rest(
        ("v0.13.0", "", "### Added\n- Caption facets", "2026-11-18T00:00:00Z", False, False)
    )
    http.releases["argus-studio"] = rest(
        old,
        ("v0.5.0", "Studio 0.5", "### Added\n- Jobs view", "2026-11-19T00:00:00Z", False, False),
        ("v0.6.0rc1", "rc", "", "2026-11-20T00:00:00Z", False, True),
    )
    events = poll(cfg, store, http, PUBS)
    assert [(e.kind, e.project, e.version) for e in events] == [
        ("collect", "argus-studio (github)", "0.5.0"),
        ("collect", "argus-studio (github)", "0.6.0rc1"),
        ("collect", "argus-lens (github)", "0.13.0"),
    ]
    http.pypi["argus-lens"]["0.13.0"] = "2026-11-18T02:00:00Z"
    poll(cfg, store, http, PUBS)
    rows = {(r["source"], r["project"], r["version"]): r for r in store.releases(open_only=True)}
    assert set(rows) == {
        ("github", "argus-studio", "0.5.0"),
        ("github", "argus-studio", "0.6.0rc1"),
        ("pypi", "argus-lens", "0.13.0"),
    }
    assert rows[("pypi", "argus-lens", "0.13.0")]["notes"] == "### Added\n- Caption facets"
    assert store.list_drafts() == []

    # a PyPI release seen first keeps its row; the GitHub twin only adds notes
    http.pypi["argus-lens"]["0.14.0"] = "2026-11-25T00:00:00Z"
    poll(cfg, store, http, PUBS)
    http.releases["argus-lens"] = rest(("v0.14.0", "", "- Faster", "2026-11-25T00:00:00Z", False, False))
    events = poll(cfg, store, http, PUBS)
    assert ("collect", "notes added to the PyPI release") in [(e.kind, e.detail) for e in events]
    assert store.release("argus-lens", "0.14.0", "github") is None
    assert store.release("argus-lens", "0.14.0")["notes"] == "- Faster"

    # the digest page lists GitHub releases with source: github and versions oldest first
    poll(
        cfg,
        store,
        http,
        PUBS,
        now=__import__("datetime").datetime(2026, 11, 30, 1, tzinfo=__import__("datetime").timezone.utc),
    )
    page = (tmp_path / "site" / "2026-11-30-digest.md").read_text(encoding="utf-8")
    assert '  - { package: argus-studio, source: github, versions: ["0.5.0", "0.6.0rc1"] }' in page
    assert '  - { package: argus-lens, versions: ["0.13.0", "0.14.0"] }' in page
    store.close()


def test_per_release_mode_drafts_github_but_not_prereleases(tmp_path):
    cfg = load_config_text(tmp_path, "github: {owner: smk762, repos: [argus-cull]}\n")
    store = Store(cfg.store_path)
    http = GH(feed=FEED.replace(b"v0.4.0", b"v0.3.0"))
    poll(cfg, store, http, PUBS)  # baseline from the feed (no token)
    http.feed = FEED.replace(b"v0.1.0", b"v0.5.0rc1")
    events = poll(cfg, store, http, PUBS)
    kinds = sorted((e.kind, e.version) for e in events)
    assert kinds == [("draft", "0.4.0"), ("skip", "0.5.0rc1")]
    drafts = store.list_drafts()
    assert {(d.project, d.version, d.post_key) for d in drafts} == {
        ("argus-cull", "0.4.0", "release:argus-cull==0.4.0")
    }
    assert drafts[0].link == "https://github.com/smk762/argus-cull/releases/tag/v0.4.0"
    store.close()


def load_config_text(tmp_path, block):
    path = tmp_path / "v.yaml"
    path.write_text(block + "destinations:\n  twitter: {enabled: true}\n  patreon: {enabled: true}\n", encoding="utf-8")
    cfg = load_config(path)
    cfg.store_path = tmp_path / "s.sqlite"
    return cfg


def test_github_config_and_seen_migration(tmp_path):
    for bad in (
        "github: {owner: 'bad owner', prefix: a}\n",
        "github: {owner: smk762}\n",
        "github: {owner: smk762, prefix: a, include_prereleases: maybe}\n",
    ):
        with pytest.raises(ConfigError):
            load_config_text(tmp_path, bad)
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE projects (name TEXT PRIMARY KEY, baselined_at TEXT NOT NULL);
        CREATE TABLE seen (project TEXT NOT NULL, version TEXT NOT NULL, seen_at TEXT NOT NULL, PRIMARY KEY (project, version));
        INSERT INTO projects VALUES ('argus-lens', 't');
        INSERT INTO seen VALUES ('argus-lens', '0.12.0', 't');
    """)
    conn.commit()
    conn.close()
    store = Store(path)
    assert store.is_baselined("argus-lens") and store.seen("argus-lens", "0.12.0")
    assert not store.seen("argus-lens", "0.12.0", "github")
    store.mark_seen("argus-lens", "0.12.0", "github")
    assert store.seen_versions("argus-lens") == ["0.12.0"]
    store.close()


def test_api_releases_beyond_the_first_page_are_listed():
    def page(start, count):
        return json.loads(
            rest(
                *[
                    (f"v0.{i}.0", "", "", f"2020-01-01T00:{i // 60:02d}:{i % 60:02d}Z", False, False)
                    for i in range(start, start + count)
                ]
            )
        )

    newest_first = page(0, 230)[::-1]

    class Paged:
        calls: list = []

        def request(self, method, url, body=None, headers=None):
            self.calls.append(url)
            number = int(url.split("page=")[-1]) if "&page=" in url else 1
            return 200, json.dumps(newest_first[(number - 1) * 100 : number * 100]).encode()

    http = Paged()
    found = list_releases(http, "smk762", "argus-studio", token="t")
    assert len(found) == 230 and len(http.calls) == 3
    assert (found[0].version, found[-1].version) == ("0.0.0", "0.229.0")


def test_github_prefix_of_only_spaces_is_no_prefix(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text(
        'pypi: {projects: [a]}\ndestinations:\n  twitter: {enabled: true}\ngithub: {owner: smk762, prefix: "  "}\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="repos"):
        load_config(path)


def test_html_to_text_keeps_a_tail_the_parser_holds_back():
    assert html_to_text("<p>Notes from R&D") == "Notes from R&D"
    assert html_to_text("<p>Now with AT&T</p> and R&D").endswith("and R&D")
