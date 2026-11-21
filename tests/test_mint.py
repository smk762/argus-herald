import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from argus_herald import mint
from argus_herald.config import ConfigError, load_config
from argus_herald.http import HttpError, LanHttpClient, check_lan_url
from argus_herald.media import clean, png_is_bare
from argus_herald.models import HANDED_OFF, PUBLISHED
from argus_herald.platforms import DiscordPublisher, PatreonPublisher, TwitterPublisher
from argus_herald.store import Store
from argus_herald.workflow import ReviewError, approve, confirm, poll, publish_draft

BASE = "http://mint.lan:8000"
ENV = {"ARGUS_MINT_URL": BASE + "/", "ARGUS_MINT_POSTER_TOKEN": "poster-token"}
PUBS = [TwitterPublisher(), DiscordPublisher(), PatreonPublisher()]


def _png(colour=(200, 60, 20), text=None) -> bytes:
    out = io.BytesIO()
    info = PngInfo()
    if text:
        info.add_text("Comment", text)
    Image.new("RGB", (8, 8), colour).save(out, "PNG", pnginfo=info if text else None)
    return out.getvalue()


def _item(teaser_id, image, **over):
    item = {
        "id": teaser_id,
        "project": "example-teasers",
        "title": "Lantern Coast",
        "number": "T1",
        "status": "approved",
        "current": True,
        "created_at": f"2026-11-18T09:00:0{len(teaser_id) % 10}Z",
        "approved_at": "2026-11-18T09:10:00Z",
        "image_url": f"/drafts/{teaser_id}/image",
        "image_sha256": hashlib.sha256(image).hexdigest(),
        "alt": "A lantern on a headland at dusk.",
        "quote": "The coast keeps its own hours.",
        "attribution": None,
        "label": "generated image",
        "posted": False,
        "posts": [],
    }
    return {**item, **over}


class MintHttp:
    """A stand-in for argus-mint: the feed, the images, and the posted route."""

    def __init__(self, items=(), images=None):
        self.items = list(items)
        self.images = images or {}
        self.posted = []
        self.down = False
        self.answer = 201
        self.requests = []

    def request(self, method, url, body=None, headers=None):
        self.requests.append((method, url))
        if self.down:
            raise HttpError(method, url, 0)
        assert url.startswith(BASE + "/")
        path = url[len(BASE) :]
        if method == "GET" and path.startswith("/posts/ready"):
            query = dict(part.split("=") for part in path.split("?", 1)[1].split("&"))
            start, size = int(query["offset"]), int(query["limit"])
            items = [i for i in self.items if "project" not in query or i["project"] == query["project"]]
            return 200, json.dumps({"items": items[start : start + size], "total": len(items)}).encode()
        if method == "GET" and path in self.images:
            return 200, self.images[path]
        if method == "POST" and path.endswith("/posted"):
            if self.answer in (200, 201):
                self.posted.append((path, json.loads(body), headers["Authorization"]))
                return self.answer, b"{}"
            return self.answer, json.dumps({"detail": "sha256 isn't this draft's image"}).encode()
        return 404, b""


def _config(tmp_path, source="sources:\n  mint: {}\n"):
    path = tmp_path / "config.yaml"
    path.write_text(
        "destinations:\n  twitter: {enabled: true}\n  discord: {enabled: true}\n  patreon: {enabled: true}\n" + source,
        encoding="utf-8",
    )
    cfg = load_config(path)
    cfg.store_path = tmp_path / "state.sqlite"
    return cfg


def _ready(tmp_path, source="sources:\n  mint: {}\n"):
    """A baselined store, and a mint that then gains one approved teaser."""
    cfg = _config(tmp_path, source)
    store = Store(cfg.store_path)
    image = _png()
    http = MintHttp()
    assert [e.kind for e in poll(cfg, store, http, PUBS, env=ENV)] == ["baseline"]
    http.items.append(_item("abc123", image))
    http.images["/drafts/abc123/image"] = image
    return cfg, store, http, image


def test_config_reads_the_mint_source_and_keeps_its_address_out(tmp_path):
    cfg = _config(tmp_path, "sources:\n  mint: {project: example-teasers, platforms: [twitter]}\n")
    assert (cfg.mint.project, cfg.mint.platforms) == ("example-teasers", ("twitter",))
    assert _config(tmp_path, "").mint is None
    assert _config(tmp_path, "sources:\n  mint: {enabled: false}\n").mint is None
    # Each bad case is otherwise valid, so it fails for the one reason it is there for.
    for bad, why in (
        ("{project: example-teasers, url: 'http://mint.lan'}", "Unknown sources.mint keys: url"),
        ("{project: 'Not An Id'}", "mint project id"),
        ("{project: example-teasers, platforms: [nowhere]}", "nowhere"),
    ):
        with pytest.raises(ConfigError, match=why):
            _config(tmp_path, f"sources:\n  mint: {bad}\n")


def test_lan_urls():
    for ok in ("https://mint.example.org", "http://localhost:8000", "http://mint.lan", "http://192.168.1.5:8000"):
        check_lan_url(ok)
    for bad in ("http://example.org", "http://8.8.8.8", "ftp://mint.lan", "https://user:pw@mint.example.org"):
        with pytest.raises(ValueError):
            check_lan_url(bad)
    with pytest.raises(ValueError):
        LanHttpClient().request("GET", "http://example.org/")


def test_a_bare_png_is_cleaned_byte_for_byte_and_one_with_text_is_not(tmp_path):
    bare, noisy = _png(), _png(text="taken at home")
    assert png_is_bare(bare) and not png_is_bare(noisy) and not png_is_bare(bare + b"extra")
    (tmp_path / "bare.png").write_bytes(bare)
    (tmp_path / "noisy.png").write_bytes(noisy)
    assert clean(tmp_path / "bare.png", tmp_path / "out").path.read_bytes() == bare
    cleaned = clean(tmp_path / "noisy.png", tmp_path / "out")
    assert b"taken at home" not in cleaned.path.read_bytes() and "Comment" in cleaned.removed


def test_first_read_baselines_then_a_new_teaser_becomes_drafts_with_its_image(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg.store_path)
    old, new = _png((1, 2, 3)), _png()
    http = MintHttp([_item("old1", old)], {"/drafts/old1/image": old, "/drafts/abc123/image": new})
    events = poll(cfg, store, http, PUBS, env=ENV)
    assert [(e.kind, e.detail) for e in events] == [("baseline", "1 teasers already approved")]
    assert store.list_drafts() == []

    http.items.insert(0, _item("abc123", new))
    events = poll(cfg, store, http, PUBS, env=ENV)
    assert [(e.kind, e.version) for e in events] == [("draft", "Lantern Coast T1")]
    drafts = store.list_drafts()
    # Discord can't carry media, so it gets no teaser draft.
    assert [(d.platform, d.kind, d.post_key, d.status) for d in drafts] == [
        ("twitter", "teaser", "mint:abc123", "pending"),
        ("patreon", "teaser", "mint:abc123", "pending"),
    ]
    assert drafts[0].body == "The coast keeps its own hours." and drafts[0].link == ""
    for draft in drafts:
        (media,) = store.media(draft.id)
        assert media["alt"] == "A lantern on a headland at dusk." and media["kind"] == "image"
        assert Path(media["path"]).read_bytes() == new
    assert poll(cfg, store, http, PUBS, env=ENV) == []  # never drafted twice


def test_superseded_posted_and_filtered_teasers_are_not_drafted(tmp_path):
    cfg, store, http, image = _ready(tmp_path, "sources:\n  mint: {platforms: [twitter]}\n")
    http.items[0]["attribution"] = "Hana"
    http.items += [_item("stale", image, current=False), _item("done", image, posted=True)]
    http.items.append({"id": "../etc", "image_url": "https://elsewhere.example/x.png"})
    events = poll(cfg, store, http, PUBS, env=ENV)
    assert [e.kind for e in events] == ["draft"]
    (draft,) = store.list_drafts()
    assert draft.platform == "twitter" and draft.body == "The coast keeps its own hours.\n— Hana"


def test_a_teaser_that_cannot_be_posted_as_reviewed_is_an_error_and_makes_no_draft(tmp_path):
    cfg, store, http, image = _ready(tmp_path)
    http.images["/drafts/abc123/image"] = _png((9, 9, 9))  # not the image the feed describes
    (event,) = poll(cfg, store, http, PUBS, env=ENV)
    assert event.kind == "error" and "sha256" in event.detail and store.list_drafts() == []

    noisy = _png(text="a comment")  # herald's strip would change it, so mint would refuse the report
    http.items[0] = _item("abc123", noisy)
    http.images["/drafts/abc123/image"] = noisy
    (event,) = poll(cfg, store, http, PUBS, env=ENV)
    assert event.kind == "error" and "cleaned copy differs" in event.detail and store.list_drafts() == []

    http.items[0] = _item("abc123", image, alt="")
    (event,) = poll(cfg, store, http, PUBS, env=ENV)
    assert event.kind == "error" and "alt text" in event.detail

    http.items[0] = _item("abc123", image)  # fixed in mint: the next poll drafts it
    http.images["/drafts/abc123/image"] = image
    assert [e.kind for e in poll(cfg, store, http, PUBS, env=ENV)] == ["draft"]


def test_mint_out_of_reach_is_a_skip_and_a_missing_address_an_error(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg.store_path)
    http = MintHttp()
    http.down = True
    assert [(e.kind, e.project) for e in poll(cfg, store, http, PUBS, env=ENV)] == [("skip", "mint")]
    (event,) = poll(cfg, store, http, PUBS, env={})
    assert event.kind == "error" and "ARGUS_MINT_URL" in event.detail
    (event,) = poll(cfg, store, http, PUBS, env={"ARGUS_MINT_URL": "http://example.org"})
    assert event.kind == "error" and "LAN" in event.detail
    assert not store.feed_baselined("mint")


def _handed_off(store, gate, platform="twitter"):
    draft = next(d for d in store.list_drafts() if d.platform == platform)
    publisher = next(p for p in PUBS if p.id == platform)
    approve(store, publisher, draft.id, None, gate=gate)
    publish_draft(store, publisher, store.get(draft.id), {}, None, gate=gate)
    assert store.get(draft.id).status == HANDED_OFF
    return draft.id


def test_confirm_with_a_url_reports_the_post_to_mint(tmp_path, gate):
    cfg, store, http, image = _ready(tmp_path)
    poll(cfg, store, http, PUBS, env=ENV)
    draft_id = _handed_off(store, gate)

    confirm(store, draft_id)  # no URL yet: mint isn't told
    assert store.mint_report(draft_id) is None
    assert "once its URL is recorded" in mint.report_note(store, store.get(draft_id), ENV, http)

    confirmed = confirm(store, draft_id, "https://twitter.com/someone/status/123")
    assert confirmed.status == PUBLISHED
    assert mint.report_note(store, confirmed, ENV, http) == "Mint has recorded the post."
    ((path, body, auth),) = http.posted
    assert path == "/drafts/abc123/posted" and auth == "Bearer poster-token"
    assert body["where"] == "twitter" and body["url"] == "https://x.com/someone/status/123"
    assert body["sha256"] == hashlib.sha256(image).hexdigest()
    assert body["at"].endswith("+00:00")

    poll(cfg, store, http, PUBS, env=ENV)  # nothing is sent twice
    assert len(http.posted) == 1


def test_a_report_waits_while_mint_is_away_and_is_sent_unchanged_later(tmp_path, gate):
    cfg, store, http, _ = _ready(tmp_path)
    poll(cfg, store, http, PUBS, env=ENV)
    draft_id = _handed_off(store, gate, "patreon")
    http.down = True
    confirmed = confirm(store, draft_id, "https://www.patreon.com/posts/lantern-1")
    assert "next poll" in mint.report_note(store, confirmed, ENV, http)
    queued = store.mint_report(draft_id)
    assert not queued["reported_at"]
    assert "next poll" in mint.report_note(store, confirmed, {}, http)  # no address or token: still queued

    http.down = False
    http.answer = 401
    assert mint.send_reports(store, ENV, http)[0].outcome == "waiting"
    http.answer = 201
    assert poll(cfg, store, http, PUBS, env=ENV) == []
    ((_, body, _),) = http.posted
    assert body["at"] == queued["at"] and body["where"] == "patreon"
    assert store.mint_report(draft_id)["reported_at"]


def test_a_refused_report_is_shown_once_and_not_retried(tmp_path, gate):
    cfg, store, http, _ = _ready(tmp_path)
    poll(cfg, store, http, PUBS, env=ENV)
    draft_id = _handed_off(store, gate)
    confirm(store, draft_id, "https://x.com/someone/status/5")
    http.answer = 409
    (event,) = poll(cfg, store, http, PUBS, env=ENV)
    assert event.kind == "error" and "sha256 isn't this draft's image" in event.detail
    assert "did not record" in mint.report_note(store, store.get(draft_id), ENV, http)
    sent = len(http.requests)
    assert poll(cfg, store, http, PUBS, env=ENV) == []
    assert [m for m, _ in http.requests[sent:]] == ["GET"]


def test_drafts_from_other_sources_are_never_reported(tmp_path, gate):
    from argus_herald.models import Post
    from argus_herald.workflow import compose

    store = Store(tmp_path / "state.sqlite")
    (draft_id,) = compose(store, [TwitterPublisher()], Post(kind="note", subject="s", text="hello", link=""))
    approve(store, PUBS[0], draft_id, None, gate=gate)
    publish_draft(store, PUBS[0], store.get(draft_id), {}, None, gate=gate)
    confirm(store, draft_id, "https://x.com/someone/status/9")
    assert store.mint_report(draft_id) is None and store.mint_reports_due() == []


def test_cli_confirm_says_where_the_report_stands(tmp_path, gate, capsys, monkeypatch):
    from argus_herald.cli import cmd_confirm

    cfg, store, http, _ = _ready(tmp_path)
    poll(cfg, store, http, PUBS, env=ENV)
    draft_id = _handed_off(store, gate)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    assert cmd_confirm(store, draft_id, "https://x.com/someone/status/77", http) == 0
    assert "Mint has recorded the post." in capsys.readouterr().out
    assert len(http.posted) == 1


def test_a_report_stuck_on_the_token_is_said_on_the_poll_and_in_the_note(tmp_path, gate):
    cfg, store, http, _ = _ready(tmp_path)
    poll(cfg, store, http, PUBS, env=ENV)
    draft_id = _handed_off(store, gate)
    confirmed = confirm(store, draft_id, "https://x.com/someone/status/5")
    no_token = {"ARGUS_MINT_URL": BASE}
    note = mint.report_note(store, confirmed, no_token, http)
    assert "ARGUS_MINT_POSTER_TOKEN" in note and "next poll" in note
    (event,) = poll(cfg, store, http, PUBS, env=no_token)
    assert event.kind == "error" and "ARGUS_MINT_POSTER_TOKEN" in event.detail
    (event,) = poll(cfg, store, http, PUBS, env={})  # the missing address is said once, not twice
    assert event.kind == "error" and "ARGUS_MINT_URL" in event.detail
    assert not store.mint_report(draft_id)["reported_at"]


def test_another_project_is_baselined_on_its_own(tmp_path):
    cfg, store, http, image = _ready(tmp_path, "sources:\n  mint: {project: one}\n")
    http.items[0]["project"] = "two"  # approved in a project this source wasn't reading
    assert poll(cfg, store, http, PUBS, env=ENV) == []
    cfg = _config(tmp_path, "sources:\n  mint: {project: two}\n")
    assert [e.kind for e in poll(cfg, store, http, PUBS, env=ENV)] == ["baseline"]
    assert poll(cfg, store, http, PUBS, env=ENV) == [] and store.list_drafts() == []


def test_a_draft_left_without_its_image_gets_it_on_the_next_poll(tmp_path, monkeypatch):
    cfg, store, http, image = _ready(tmp_path)
    real = mint.attach_media

    def busy(*args, **kwargs):
        raise ReviewError("the store is busy")

    monkeypatch.setattr(mint, "attach_media", busy)
    (event,) = poll(cfg, store, http, PUBS, env=ENV)
    assert event.kind == "error"
    left = store.list_drafts()
    assert [d.platform for d in left] == ["twitter", "patreon"]  # the drafts exist, without their image
    assert all(not store.media(d.id) for d in left)
    monkeypatch.setattr(mint, "attach_media", real)
    (event,) = poll(cfg, store, http, PUBS, env=ENV)
    assert event.kind == "draft" and event.draft_ids == []  # no new drafts: the same ones are finished
    assert [d.id for d in store.list_drafts()] == [d.id for d in left]
    assert all(len(store.media(d.id)) == 1 for d in left)


def test_a_teaser_id_with_a_trailing_newline_is_skipped(tmp_path):
    cfg, store, http, image = _ready(tmp_path)
    # "$" alone lets the newline through, into a file name under the media folder and mint's URLs.
    http.items[0] = _item("abc123\n", image, image_url="/drafts/abc123/image")
    assert poll(cfg, store, http, PUBS, env=ENV) == []
    assert store.list_drafts() == [] and not (cfg.media_root / "mint").exists()


def test_an_image_path_with_a_tab_is_skipped_not_fetched(tmp_path):
    cfg, store, http, image = _ready(tmp_path)
    # URL parsers drop tabs and newlines, so this would read as "//elsewhere.example/x.png".
    http.items[0] = _item("abc123", image, image_url="/\t/elsewhere.example/x.png")
    http.images["/\t/elsewhere.example/x.png"] = image
    assert poll(cfg, store, http, PUBS, env=ENV) == []
    assert store.list_drafts() == [] and all("elsewhere" not in url for _, url in http.requests)


def test_a_report_with_a_bad_draft_id_is_refused_for_that_reason(tmp_path):
    from argus_herald.models import Post
    from argus_herald.workflow import compose

    store = Store(tmp_path / "state.sqlite")
    (draft_id,) = compose(store, [TwitterPublisher()], Post(kind="teaser", subject="s", text="t", link=""))
    store.add_mint_report(draft_id, mint_id="../abc", platform="twitter", url=None, at="2026-11-18", sha256="0" * 64)
    (reported,) = mint.send_reports(store, ENV, MintHttp())
    assert reported.outcome == "refused" and reported.detail == "'../abc' isn't a draft id mint accepts"
