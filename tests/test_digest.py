import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from conftest import DENY_TERM

from argus_herald.config import ConfigError, load_config
from argus_herald.digest import (
    build_digest,
    closed_windows,
    current_window,
    highlight,
    is_prerelease,
    period_label,
    promote,
    suggest_headline,
    window_at,
)
from argus_herald.platforms import PatreonPublisher, TwitterPublisher
from argus_herald.store import Store
from argus_herald.workflow import poll

ADDED = "### Added\n- Caption facets for every image\n- Faster index\n\n### Fixed\n- A crash\n"


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _config(tmp_path, extra=""):
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    path = tmp_path / "c.yaml"
    path.write_text(
        "pypi: {projects: [argus-atlas]}\n"
        "destinations:\n  twitter: {enabled: true}\n  patreon: {enabled: true}\n"
        f"digest:\n  anchor: 2026-11-16\n  window_days: 14\n  site_url: https://example.org/updates\n  site_dir: {site}\n"
        + extra,
        encoding="utf-8",
    )
    cfg = load_config(path)
    cfg.store_path = tmp_path / "s.sqlite"
    return cfg


class PyPI:
    def __init__(self, uploads):
        self.uploads = uploads  # version -> ISO upload time

    def request(self, method, url, body=None, headers=None):
        if url == "https://pypi.org/pypi/argus-atlas/json" or url.startswith("https://pypi.org/pypi/argus-atlas/"):
            data = {
                "info": {
                    "name": "argus-atlas",
                    "version": list(self.uploads)[-1],
                    "summary": "Bake tiles",
                    "project_urls": {},
                },
                "releases": {v: [{"yanked": False, "upload_time_iso_8601": t}] for v, t in self.uploads.items()},
            }
            return 200, json.dumps(data).encode()
        return 404, b"{}"


PUBS = [TwitterPublisher(), PatreonPublisher()]


def test_windows_and_labels():
    from argus_herald.config import DigestSettings

    s = DigestSettings(anchor=date(2026, 11, 16))
    assert window_at(s, 0).end == date(2026, 11, 30) and window_at(s, 0).slug == "2026-11-30-digest"
    assert current_window(s, at("2026-12-01T09:00:00")).start == date(2026, 11, 30)
    assert [w.end for w in closed_windows(s, at("2026-12-15T00:00:00"))] == [date(2026, 11, 30), date(2026, 12, 14)]
    assert closed_windows(s, at("2026-11-29T23:59:59")) == []
    assert period_label(window_at(s, 0)) == "16–29 Nov 2026"
    assert period_label(window_at(s, 1)) == "30 Nov – 13 Dec 2026"
    assert period_label(window_at(s, 3)) == "28 Dec 2026 – 10 Jan 2027"


@pytest.mark.parametrize(
    "version, earlier, notes, expected",
    [
        ("0.13.0", ["0.12.0", "0.12.1"], ADDED, True),
        ("1.0.0", ["0.9.3"], ADDED, True),
        ("0.12.2", ["0.12.0", "0.12.1"], ADDED, False),  # patch
        ("0.13.0", ["0.12.0"], "### Fixed\n- A crash\n", False),  # nothing added
        ("0.13.0rc1", ["0.12.0"], ADDED, False),  # prerelease
        ("0.1.0", [], "- Added the first CLI\n", True),  # first release
        ("0.13.0", ["0.12.0", "0.13.0rc1"], ADDED, True),  # rc doesn't count as previous
    ],
)
def test_suggest_headline(version, earlier, notes, expected):
    assert suggest_headline(version, earlier, notes) is expected


def test_highlight_and_prerelease():
    assert highlight(ADDED) == "Caption facets for every image"
    assert highlight("- see [the docs](https://x.org) for **more**.\n") == "see the docs for more"
    assert highlight("", "Bake tiles") == "Bake tiles"
    wrapped = "### Added\n- Image-to-image. A request may carry `init_image`\n  (PNG, at most 16 MiB).\n- Second\n"
    assert highlight(wrapped) == "Image-to-image"
    assert highlight("- `serve` binds `127.0.0.1`. Pass `--host` to\n  change it.\n") == "`serve` binds `127.0.0.1`"
    long = "- " + "word " * 60 + "\n"
    assert highlight(long).endswith("word…") and len(highlight(long)) <= 120
    assert [is_prerelease(v) for v in ("0.3.0rc1", "1.0.0a2", "2.0.0.dev1", "0.3.0", "1.2.3.post1")] == [
        True,
        True,
        True,
        False,
        False,
    ]


def test_window_close_drafts_one_page_and_one_draft_per_platform(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg.store_path)
    http = PyPI({"0.1.0": "2026-11-01T00:00:00Z"})
    poll(cfg, store, http, PUBS, now=at("2026-11-17T00:00:00"))  # baseline

    http.uploads.update({"0.2.0": "2026-11-18T10:00:00Z", "0.2.1": "2026-11-20T10:00:00Z"})
    events = poll(cfg, store, http, PUBS, now=at("2026-11-21T00:00:00"))
    assert [(e.kind, e.version) for e in events] == [("collect", "0.2.0"), ("collect", "0.2.1")]
    assert store.list_drafts() == []

    events = poll(cfg, store, http, PUBS, now=at("2026-11-30T01:00:00"))
    assert [e.kind for e in events] == ["digest"]
    drafts = store.list_drafts()
    assert [(d.kind, d.platform) for d in drafts] == [("digest", "twitter"), ("digest", "patreon")]
    assert drafts[0].link == "https://example.org/updates/2026-11-30-digest/"
    assert drafts[0].body.startswith("Argus digest, 16–29 Nov 2026: 2 releases across 1 package.")
    assert drafts[0].body.endswith("https://example.org/updates/2026-11-30-digest/")
    page = (tmp_path / "site" / "2026-11-30-digest.md").read_text(encoding="utf-8")
    assert "date: 2026-11-30\nkind: digest\n" in page
    assert "period: { from: 2026-11-16, to: 2026-11-29 }" in page
    assert '  - { package: argus-atlas, versions: ["0.2.0", "0.2.1"] }' in page
    assert "\ndraft: true\n---\n" in page

    assert poll(cfg, store, http, PUBS, now=at("2026-12-01T00:00:00")) == []  # no second digest

    # Released inside the closed window but seen after it closed: goes to the next digest.
    http.uploads["0.2.2"] = "2026-11-29T23:00:00Z"
    poll(cfg, store, http, PUBS, now=at("2026-12-02T00:00:00"))
    events = poll(cfg, store, http, PUBS, now=at("2026-12-14T00:00:01"))
    second = store.digests()[0]
    assert [r["version"] for r in store.releases(digest_id=second["id"])] == ["0.2.2"]
    store.close()


def _digest_with(store, rows):
    for version, when, notes in rows:
        store.add_release(
            project="argus-lens",
            version=version,
            released_at=when,
            summary="Index images",
            notes=notes,
            url=f"https://pypi.org/project/argus-lens/{version}/",
        )
        store.mark_seen("argus-lens", version)
    return store.create_digest("2026-11-16", "2026-11-29", "2026-11-30T00:00:00+00:00")


def test_exclude_and_rebuild_keep_edits(tmp_path, gate):
    cfg = _config(tmp_path)
    store = Store(cfg.store_path)
    store.mark_seen("argus-lens", "0.12.0")
    digest_id = _digest_with(
        store,
        [("0.13.0", "2026-11-18T00:00:00+00:00", ADDED), ("0.13.1", "2026-11-19T00:00:00+00:00", "- Fixed a crash\n")],
    )
    ids, status = build_digest(store, cfg.digest, PUBS, digest_id, gate)
    assert len(ids) == 2 and status.startswith("wrote ")
    x = store.get(ids[0])
    assert "• lens 0.13.0: Caption facets for every image" in x.body
    page_path = tmp_path / "site" / "2026-11-30-digest.md"
    assert "*(suggested headline)*" in page_path.read_text(encoding="utf-8")

    store.update_text(ids[1], "My title", "My own words", ("pending",))  # a person edits Patreon
    store.set_excluded(store.release("argus-lens", "0.13.1")["id"], True)
    _ids, status = build_digest(store, cfg.digest, PUBS, digest_id, gate)
    assert status.startswith("wrote ")
    assert "1 release across 1 package" in store.get(ids[0]).body
    assert store.get(ids[1]).body == "My own words"
    assert '"0.13.1"' not in page_path.read_text(encoding="utf-8")

    page_path.write_text(page_path.read_text(encoding="utf-8") + "\nA person wrote this.\n", encoding="utf-8")
    store.set_excluded(store.release("argus-lens", "0.13.1")["id"], False)
    _ids, status = build_digest(store, cfg.digest, PUBS, digest_id, gate)
    assert status.startswith("left alone")
    assert page_path.read_text(encoding="utf-8").endswith("A person wrote this.\n")
    store.close()


def test_privacy_gate_blocks_the_page_but_not_the_drafts(tmp_path, gate):
    cfg = _config(tmp_path)
    store = Store(cfg.store_path)
    digest_id = _digest_with(store, [("0.13.0", "2026-11-18T00:00:00+00:00", f"### Added\n- Works with {DENY_TERM}\n")])
    ids, status = build_digest(store, cfg.digest, PUBS, digest_id, gate)
    assert status == "refused by the privacy gate; page not written" and len(ids) == 2
    assert not (tmp_path / "site" / "2026-11-30-digest.md").exists()
    assert "deny-listed term" in store.digest(digest_id)["error"]
    _ids, status = build_digest(store, cfg.digest, PUBS, digest_id, None)
    assert status == "privacy gate unavailable; page not written"
    store.close()


def test_promote_makes_a_headline_group_and_optional_release_page(tmp_path, gate):
    cfg = _config(tmp_path)
    store = Store(cfg.store_path)
    _digest_with(store, [("0.13.0", "2026-11-18T00:00:00+00:00", ADDED)])
    row = store.release("argus-lens", "0.13.0")
    ids, status = promote(store, cfg.digest, PUBS, row, site_page=True, gate=gate)
    drafts = [store.get(i) for i in ids]
    assert {(d.kind, d.post_key) for d in drafts} == {("headline", "headline:argus-lens==0.13.0")}
    assert status.startswith("wrote ") and "kind: release" in Path(status.split(" ", 1)[1]).read_text()
    assert promote(store, cfg.digest, PUBS, row)[0] == []  # idempotent
    store.close()


def test_digest_config_is_validated(tmp_path):
    assert load_config_text(tmp_path, "digest: {mode: per_release}\n").digest is None
    for bad in (
        "digest: {anchor: soon}\n",
        "digest: {anchor: 2026-11-16, window_days: 0}\n",
        "digest: {anchor: 2026-11-16, site_url: http://x.org}\n",
        "digest: {anchor: 2026-11-16, platforms: [discord]}\n",
    ):
        with pytest.raises(ConfigError):
            load_config_text(tmp_path, bad)


def load_config_text(tmp_path, block):
    path = tmp_path / "v.yaml"
    path.write_text("pypi: {projects: [a]}\ndestinations:\n  twitter: {enabled: true}\n" + block, encoding="utf-8")
    return load_config(path)


def test_cli_digest_status_exclude_and_promote(tmp_path, capsys):
    from argus_herald.cli import main

    cfg_path = tmp_path / "c.yaml"
    _config(tmp_path)  # writes c.yaml and the site dir
    store = Store(tmp_path / "s.sqlite")
    store.add_release(
        project="argus-lens",
        version="0.13.0",
        released_at="2099-01-01T00:00:00+00:00",
        summary="",
        notes=ADDED,
        url="https://pypi.org/project/argus-lens/0.13.0/",
    )
    store.close()
    base = ["--config", str(cfg_path), "--env", str(tmp_path / "no.env"), "--store", str(tmp_path / "s.sqlite")]
    assert main([*base, "digest"]) == 0
    out = capsys.readouterr().out
    assert "Open window" in out and "argus-lens==0.13.0 [suggested headline]" in out
    assert main([*base, "digest", "exclude", "argus-lens==0.13.0"]) == 0
    assert main([*base, "digest", "exclude", "argus-lens==9.9"]) == 2
    assert main([*base, "promote", "argus-lens==0.13.0"]) == 0
    assert "headline drafts: #1, #2" in capsys.readouterr().out


def test_cli_promote_finds_a_github_only_release(tmp_path, capsys):
    from argus_herald.cli import main

    _config(tmp_path)
    store = Store(tmp_path / "s.sqlite")
    store.add_release(
        project="argus-herald",
        version="0.2.0",
        released_at="2099-01-01T00:00:00+00:00",
        summary="",
        notes=ADDED,
        url="https://github.com/smk762/argus-herald/releases/tag/v0.2.0",
        source="github",
    )
    store.close()
    base = [
        "--config",
        str(tmp_path / "c.yaml"),
        "--env",
        str(tmp_path / "no.env"),
        "--store",
        str(tmp_path / "s.sqlite"),
    ]
    assert main([*base, "promote", "argus-herald==0.2.0"]) == 0
    assert "headline drafts: #" in capsys.readouterr().out


def test_notable_names_the_newest_release_of_a_package(tmp_path):
    from argus_herald.digest import notable

    store = Store(tmp_path / "s.sqlite")
    # Reversed character by character, the older stamp sorts first; the newer one must win.
    for version, when in (("0.4.1", "2026-11-17T00:00:09+00:00"), ("0.4.2", "2026-11-25T00:00:01+00:00")):
        store.add_release(
            project="argus-atlas",
            version=version,
            released_at=when,
            summary="",
            notes=f"### Fixed\n- Fix in {version}\n",
            url="",
        )
    for version, when in (("0.2.1", "2026-11-18T10:00:00+00:00"), ("0.2.2", "2026-11-19T08:00:05+00:00")):
        store.add_release(
            project="argus-lens",
            version=version,
            released_at=when,
            summary="",
            notes=f"### Fixed\n- Fix in {version}\n",
            url="",
        )
    picked = notable(store, store.releases(open_only=True))
    assert [(row["project"], row["version"]) for row, _line, _suggested in picked] == [
        ("argus-atlas", "0.4.2"),
        ("argus-lens", "0.2.2"),
    ]
    store.close()


@pytest.mark.parametrize("slug", ["../../escaped", "/abs/escaped", "2026-11-18-pkg/sub-1-0", "..", ".hidden"])
def test_release_page_stays_inside_the_site_dir(tmp_path, gate, slug):
    from argus_herald.digest import _write_release_page

    cfg = _config(tmp_path)
    site = Path(cfg.digest.site_dir)
    status = _write_release_page(cfg.digest, slug, "page text", gate)
    written = [p for p in tmp_path.rglob("*.md")]
    assert all(p.parent == site and not p.name.startswith(".") for p in written), status
    assert not (tmp_path / "escaped.md").exists() and not Path("/abs/escaped.md").exists()
    assert status.startswith("wrote ") == bool(written)
