import re

import pytest
from starlette.testclient import TestClient

from pip_release.cli import main
from pip_release.config import load_config
from pip_release.store import Store
from pip_release.platforms import load_publishers
from pip_release.ui.app import account_links, assert_ui_bind, create_app


def _config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
pypi:
  user: dragonhound_dev
  projects: [argus-atlas]
destinations:
  twitter:
    enabled: true
    username: dragonhound3
  patreon:
    enabled: true
    name: AtlasRealms
    url: https://www.patreon.com/c/AtlasRealms
""",
        encoding="utf-8",
    )
    config = load_config(path)
    config.store_path = tmp_path / "state.sqlite"
    return config


def _seed(config) -> None:
    store = Store(config.store_path)
    store.add_draft(
        project="argus-atlas",
        version="0.2.0",
        platform="twitter",
        title="",
        body="argus-atlas 0.2.0 is out.\n\nhttps://pypi.org/project/argus-atlas/0.2.0/",
        link="https://pypi.org/project/argus-atlas/0.2.0/",
    )
    store.add_draft(
        project="argus-atlas",
        version="0.2.0",
        platform="patreon",
        title="argus-atlas 0.2.0",
        body="argus-atlas 0.2.0 is out.\n\nhttps://pypi.org/project/argus-atlas/0.2.0/",
        link="https://pypi.org/project/argus-atlas/0.2.0/",
    )
    store.close()


def _app(tmp_path, token: str = ""):
    config = _config(tmp_path)
    _seed(config)
    import os

    env = {
        "PIP_RELEASE_UI_SECRET": "test-secret",
        "PIP_RELEASE_OPSEC_DENYLIST": os.environ["PIP_RELEASE_OPSEC_DENYLIST"],
        "AI_AUDIT_OPSEC_DENYLIST": os.environ["AI_AUDIT_OPSEC_DENYLIST"],
    }
    if token:
        env["PIP_RELEASE_UI_TOKEN"] = token
    return create_app(config, env), config


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def test_queue_lists_the_release_and_save_edits_the_draft(tmp_path):
    app, config = _app(tmp_path)
    client = TestClient(app)
    page = client.get("/")
    assert page.status_code == 200
    assert "argus-atlas" in page.text
    assert "0.2.0" in page.text
    assert "Approve 2 pending" in page.text
    assert 'href="https://pypi.org/user/dragonhound_dev/"' in page.text
    assert 'href="https://x.com/dragonhound3"' in page.text
    assert 'href="https://www.patreon.com/c/AtlasRealms"' in page.text
    draft_page = client.get("/drafts/1")
    assert 'href="https://x.com/dragonhound3"' in draft_page.text

    draft = client.get("/drafts/1")
    assert "Post" in draft.text
    updated = "argus-atlas 0.2.0 is out. Smaller note.\n\nhttps://pypi.org/project/argus-atlas/0.2.0/"
    saved = client.post(
        "/drafts/1/save",
        data={"csrf": _csrf(draft.text), "title": "", "body": updated},
        follow_redirects=True,
    )
    assert saved.status_code == 200
    assert "Saved" in saved.text
    store = Store(config.store_path)
    assert store.get(1).body == updated
    assert store.get(1).status == "pending"
    store.close()


def test_approve_publish_and_confirm_patreon(tmp_path):
    app, config = _app(tmp_path)
    client = TestClient(app)
    draft = client.get("/drafts/2")
    client.post(
        "/drafts/2/approve",
        data={"csrf": _csrf(draft.text), "title": "argus-atlas 0.2.0", "body": store_body()},
        follow_redirects=True,
    )
    publish = client.post(
        "/drafts/2/publish",
        data={"csrf": _csrf(client.get("/drafts/2").text)},
        follow_redirects=True,
    )
    assert "Paste into Patreon" in publish.text
    assert "patreon.com/c/AtlasRealms" in publish.text
    confirmed = client.post(
        "/drafts/2/confirm",
        data={"csrf": _csrf(publish.text)},
        follow_redirects=True,
    )
    assert "published on Patreon" in confirmed.text
    store = Store(config.store_path)
    assert store.get(2).status == "published"
    open_queue = client.get("/?status=open")
    assert "/drafts/2" not in open_queue.text
    store.close()


def test_reject_and_missing_csrf(tmp_path):
    app, _config = _app(tmp_path)
    client = TestClient(app)
    draft = client.get("/drafts/1")
    rejected = client.post(
        "/drafts/1/reject",
        data={"csrf": _csrf(draft.text), "note": "hold"},
        follow_redirects=True,
    )
    assert "Rejected" in rejected.text
    assert "hold" in rejected.text
    stale = client.post("/drafts/1/approve", data={"title": "", "body": "x"})
    assert stale.status_code == 400


def test_token_gates_the_queue(tmp_path):
    app, _config = _app(tmp_path, token="s3cret")
    client = TestClient(app, follow_redirects=False)
    assert client.get("/").headers["location"] == "/login"
    login = client.get("/login")
    bad = client.post("/login", data={"csrf": _csrf(login.text), "token": "nope"})
    assert bad.status_code == 401
    good = client.post(
        "/login",
        data={"csrf": _csrf(bad.text), "token": "s3cret"},
        follow_redirects=False,
    )
    assert good.status_code == 303
    assert client.get("/").status_code == 200


def test_account_links_skip_disabled_channels_and_unsafe_urls(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
pypi:
  projects: [argus-atlas]
destinations:
  twitter:
    enabled: true
    username: "@dragonhound3"
  discord:
    enabled: false
    url: https://discord.com/channels/1/2
  telegram:
    enabled: true
    username: atlas_news
    url: "javascript:alert(1)"
  patreon:
    enabled: true
    name: AtlasRealms
    url: https://user:secret@www.patreon.com/c/AtlasRealms
""",
        encoding="utf-8",
    )
    config = load_config(path)
    links = account_links(config, {item.id: item for item in load_publishers()})
    assert links["profiles"] == [
        {"label": "argus-atlas", "href": "https://pypi.org/project/argus-atlas/"}
    ]
    hrefs = [item["href"] for item in links["destinations"]]
    assert hrefs == [
        "https://x.com/dragonhound3",
        "https://t.me/atlas_news",
        "https://www.patreon.com/c/AtlasRealms",
    ]
    assert all("discord" not in href for href in hrefs)
    assert all("secret" not in href for href in hrefs)


def test_public_bind_requires_a_token():
    with pytest.raises(PermissionError, match="PIP_RELEASE_UI_TOKEN"):
        assert_ui_bind("0.0.0.0", {})
    assert_ui_bind("127.0.0.1", {})
    assert_ui_bind("0.0.0.0", {"PIP_RELEASE_UI_TOKEN": "x"})


def test_cli_refuses_a_public_ui_without_a_token(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("pypi:\n  projects: [demo]\ndestinations: {}\n", encoding="utf-8")
    code = main(
        [
            "--config",
            str(path),
            "--env",
            str(tmp_path / "missing.env"),
            "ui",
            "--host",
            "0.0.0.0",
        ]
    )
    assert code == 2
    assert "PIP_RELEASE_UI_TOKEN" in capsys.readouterr().err


def store_body() -> str:
    return "argus-atlas 0.2.0 is out.\n\nhttps://pypi.org/project/argus-atlas/0.2.0/"


def test_x_draft_hands_off_to_an_intent_link_and_records_the_post_url(tmp_path):
    app, config = _app(tmp_path)
    client = TestClient(app)
    page = client.get("/drafts/1")
    body = Store(config.store_path).get(1).body
    client.post("/drafts/1/approve", data={"csrf": _csrf(page.text), "title": "", "body": body}, follow_redirects=True)
    publish = client.post("/drafts/1/publish", data={"csrf": _csrf(client.get("/drafts/1").text)}, follow_redirects=True)
    assert "ready to post on Twitter / X" in publish.text
    assert 'href="https://x.com/intent/post?text=' in publish.text
    assert 'data-copy="handoff-text"' in publish.text

    bad = client.post("/drafts/1/confirm", data={"csrf": _csrf(publish.text), "url": "https://example.com/x"}, follow_redirects=True)
    assert "https://x.com/&lt;user&gt;/status/&lt;id&gt;" in bad.text
    good = client.post(
        "/drafts/1/confirm",
        data={"csrf": _csrf(bad.text), "url": "https://x.com/dragonhound3/status/777"},
        follow_redirects=True,
    )
    assert "with its post URL" in good.text
    store = Store(config.store_path)
    assert (store.get(1).status, store.get(1).external_id) == ("published", "https://x.com/dragonhound3/status/777")
    store.close()


def _put_hit(config):
    store = Store(config.store_path)
    store.update_text(1, "", "argus-atlas 0.2.0, ask bob@corp.test", ("pending",))
    store.close()


def test_privacy_hit_is_marked_and_blocks_approve_until_accepted(tmp_path):
    app, config = _app(tmp_path)
    _put_hit(config)
    client = TestClient(app)
    page = client.get("/drafts/1")
    assert "Refused by the privacy gate" in page.text
    assert '<mark title="email address">bob@corp.test</mark>' in page.text
    assert 'formaction="/drafts/1/approve" disabled' in page.text

    refused = client.post("/drafts/1/approve", data={"csrf": _csrf(page.text)}, follow_redirects=True)
    assert "The privacy gate refused this draft" in refused.text
    no_note = client.post(
        "/drafts/1/accept", data={"csrf": _csrf(refused.text), "key": "email:bob@corp.test", "note": ""}, follow_redirects=True
    )
    assert "needs a note" in no_note.text
    accepted = client.post(
        "/drafts/1/accept",
        data={"csrf": _csrf(no_note.text), "key": "email:bob@corp.test", "note": "public support address"},
        follow_redirects=True,
    )
    assert "Refused by the privacy gate" not in accepted.text
    assert "1 accepted privacy hit" in accepted.text
    approved = client.post("/drafts/1/approve", data={"csrf": _csrf(accepted.text)}, follow_redirects=True)
    assert "Approved twitter" in approved.text


def test_missing_denylist_disables_approve_and_publish(tmp_path):
    app, config = _app(tmp_path)
    import os

    os.remove(os.environ["PIP_RELEASE_OPSEC_DENYLIST"])
    client = TestClient(app)
    page = client.get("/drafts/1")
    assert "Privacy gate unavailable" in page.text
    assert 'formaction="/drafts/1/approve" disabled' in page.text
    refused = client.post("/drafts/1/approve", data={"csrf": _csrf(page.text)}, follow_redirects=True)
    assert "deny-list is missing" in refused.text
    store = Store(config.store_path)
    assert store.get(1).status == "pending"
    store.close()


def test_bulk_approve_skips_refused_drafts(tmp_path):
    app, config = _app(tmp_path)
    _put_hit(config)
    client = TestClient(app)
    page = client.get("/")
    result = client.post(
        "/releases/approve",
        data={"csrf": _csrf(page.text), "project": "argus-atlas", "version": "0.2.0"},
        follow_redirects=True,
    )
    assert "Approved 1 draft" in result.text and "refused twitter" in result.text
    store = Store(config.store_path)
    assert (store.get(1).status, store.get(2).status) == ("pending", "approved")
    store.close()


def test_compose_form_creates_a_post_group_in_the_queue(tmp_path):
    app, config = _app(tmp_path)
    client = TestClient(app)
    form = client.get("/compose")
    assert 'name="platform" value="twitter" checked' in form.text
    missing = client.post("/compose", data={"csrf": _csrf(form.text), "kind": "demo", "text": "Hex map clip"}, follow_redirects=True)
    assert "Pick at least one destination" in missing.text and "Hex map clip" in missing.text
    made = client.post(
        "/compose",
        data={"csrf": _csrf(missing.text), "kind": "demo", "subject": "Realms demo", "text": "Hex map clip",
              "link": "https://example.org/realms", "platform": ["twitter", "patreon"]},
        follow_redirects=True,
    )
    assert "Created 2 pending drafts" in made.text
    assert "Realms demo" in made.text and 'class="kind">demo<' in made.text
    store = Store(config.store_path)
    post = [d for d in store.list_drafts() if d.kind == "demo"]
    store.close()
    assert len(post) == 2 and post[0].post_key == post[1].post_key
    approved = client.post(
        "/releases/approve", data={"csrf": _csrf(made.text), "post_key": post[0].post_key}, follow_redirects=True
    )
    assert "Approved 2 drafts for Realms demo" in approved.text


def test_digests_page_lists_releases_and_actions(tmp_path):
    from pip_release.config import DigestSettings
    from datetime import date

    app, config = _app(tmp_path)
    off = TestClient(app).get("/digests")
    assert "Digest mode is off" in off.text

    config.digest = DigestSettings(anchor=date(2026, 11, 16), site_url="https://example.org/updates/", site_dir=tmp_path)
    store = Store(config.store_path)
    store.mark_seen("argus-lens", "0.12.0")
    store.add_release(project="argus-lens", version="0.13.0", released_at="2026-11-18T00:00:00+00:00",
                      summary="", notes="### Added\n- Caption facets\n", url="https://pypi.org/project/argus-lens/0.13.0/")
    digest_id = store.create_digest("2026-11-16", "2026-11-29", "2026-11-30T00:00:00+00:00")
    rid = store.release("argus-lens", "0.13.0")["id"]
    store.close()
    client = TestClient(app)
    page = client.get("/digests")
    assert "16–29 Nov 2026" in page.text and "suggested headline" in page.text and "Caption facets" in page.text

    rebuilt = client.post(f"/digests/rebuild/{digest_id}", data={"csrf": _csrf(page.text)}, follow_redirects=True)
    assert "Rebuilt: 2 new drafts" in rebuilt.text and (tmp_path / "2026-11-30-digest.md").exists()
    dropped = client.post(f"/digests/exclude/{rid}", data={"csrf": _csrf(rebuilt.text)}, follow_redirects=True)
    assert "Excluded argus-lens 0.13.0. Rebuild" in dropped.text and "badge-off" in dropped.text
    promoted = client.post(f"/digests/promote/{rid}", data={"csrf": _csrf(dropped.text)}, follow_redirects=True)
    assert "Promoted argus-lens 0.13.0: 2 headline drafts." in promoted.text


def test_logged_posts_page_add_edit_delete(tmp_path):
    app, config = _app(tmp_path)
    client = TestClient(app)
    page = client.get("/broadcasts")
    bad = client.post("/broadcasts", data={"csrf": _csrf(page.text), "platform": "reddit", "url": "http://x.org"})
    assert bad.status_code == 400 and "plain https://" in bad.text and 'value="reddit"' in bad.text
    added = client.post("/broadcasts", data={"csrf": _csrf(bad.text), "platform": "reddit",
                                             "url": "https://www.reddit.com/r/gis/comments/abc/", "kind": "demo",
                                             "subject": "r/gis post"}, follow_redirects=True)
    assert "Logged the reddit post." in added.text and "r/gis post" in added.text
    edited = client.post("/broadcasts/edit/1", data={"csrf": _csrf(added.text), "subject": "Atlas on r/gis"}, follow_redirects=True)
    assert "Atlas on r/gis" in edited.text
    deleted = client.post("/broadcasts/delete/1", data={"csrf": _csrf(edited.text)}, follow_redirects=True)
    assert "Deleted." in deleted.text and "Atlas on r/gis" not in deleted.text


def test_media_upload_alt_serve_and_handoff(tmp_path):
    from io import BytesIO
    from PIL import Image

    app, config = _app(tmp_path)
    client = TestClient(app)
    page = client.get("/drafts/1")
    assert "Add an image, GIF or clip" in page.text
    buf = BytesIO()
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    Image.new("RGB", (16, 16), (0, 90, 0)).save(buf, "JPEG", exif=exif)
    added = client.post("/drafts/1/media", data={"csrf": _csrf(page.text), "alt": "", "all_drafts": "1"},
                        files={"file": ("my photo.jpg", buf.getvalue(), "image/jpeg")}, follow_redirects=True)
    assert "Attached my-photo.jpg to 2 drafts. Metadata removed: EXIF" in added.text
    assert "my-photo.jpg needs alt text" in added.text and 'formaction="/drafts/1/approve" disabled' in added.text
    store = Store(config.store_path)
    media_id = store.media(1)[0]["id"]
    store.close()
    served = client.get(f"/media/{media_id}")
    assert served.status_code == 200 and b"PhoneMaker" not in served.content
    saved = client.post(f"/media/{media_id}/alt", data={"csrf": _csrf(added.text), "alt": "A green square"}, follow_redirects=True)
    assert "Alt text saved." in saved.text and "needs alt text" not in saved.text
    client.post("/drafts/1/approve", data={"csrf": _csrf(saved.text)}, follow_redirects=True)
    published = client.post("/drafts/1/publish", data={"csrf": _csrf(client.get("/drafts/1").text)}, follow_redirects=True)
    assert "Attach these files" in published.text and f'href="/media/{media_id}" download="my-photo.jpg"' in published.text
    assert client.get("/media/99999").status_code == 404


def test_reply_is_editable_and_x_shows_the_reply_step(tmp_path):
    from pip_release.config import Audience

    app, config = _app(tmp_path)
    store = Store(config.store_path)
    store.conn.execute("UPDATE drafts SET reply = 'https://pypi.org/project/argus-atlas/0.2.0/' WHERE id = 1")
    store.close()
    client = TestClient(app)
    page = client.get("/drafts/1")
    assert 'name="reply"' in page.text
    saved = client.post("/drafts/1/save", data={"csrf": _csrf(page.text), "title": "", "body": "Painted coastlines",
                                                "reply": "More: https://example.org/a"}, follow_redirects=True)
    assert "More: https://example.org/a" in saved.text
    client.post("/drafts/1/approve", data={"csrf": _csrf(saved.text)}, follow_redirects=True)
    published = client.post("/drafts/1/publish", data={"csrf": _csrf(client.get("/drafts/1").text)}, follow_redirects=True)
    assert "a second step to post the link as a reply" in published.text
    done = client.post("/drafts/1/confirm", data={"csrf": _csrf(published.text), "url": "https://x.com/dragonhound3/status/55"},
                       follow_redirects=True)
    assert "Post the link as a reply" in done.text and "in_reply_to=55" in done.text
