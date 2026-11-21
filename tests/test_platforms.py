import json

import pytest

from argus_herald.config import Audience, Destination
from argus_herald.models import Draft, Release
from argus_herald.platforms import DiscordPublisher, PatreonPublisher, TelegramPublisher, TwitterPublisher
from argus_herald.textutil import html_text


def _draft(**fields) -> Draft:
    base = {
        "id": 1,
        "project": "argus-atlas",
        "version": "0.2.0",
        "platform": "twitter",
        "title": "",
        "body": "argus-atlas 0.2.0 is out.",
        "link": "https://pypi.org/project/argus-atlas/0.2.0/",
        "status": "approved",
        "edited": False,
        "created_at": "2026-11-17T00:00:00+00:00",
        "updated_at": "2026-11-17T00:00:00+00:00",
        "note": None,
        "external_id": None,
        "error": None,
    }
    return Draft(**{**base, **fields})


def test_configure_returns_a_publisher_of_its_own():
    for shared in (PatreonPublisher(), TwitterPublisher(), TwitterPublisher("api")):
        first = shared.configure(Destination(audience=Audience(hashtags=["maps"])))
        second = shared.configure(Destination(audience=Audience(hashtags=["python"])))
        assert first is not shared and second is not first
        assert first.audience.hashtags == ["maps"] and second.audience.hashtags == ["python"]
        assert shared.audience is None and shared.schedule is None
        assert type(first) is type(shared) and first.id == shared.id
    assert TwitterPublisher().configure(Destination(mode="api")).manual is False


def test_reply_link_is_offered_for_a_post_made_through_the_api():
    x = TwitterPublisher("api")
    link = "https://example.org/updates/"
    by_api = x.reply_url(_draft(external_id="1790000000000000000", reply=link))
    by_hand = x.reply_url(_draft(external_id="https://x.com/someone/status/1790000000000000000", reply=link))
    assert by_api == by_hand and "in_reply_to=1790000000000000000" in by_api
    assert x.reply_url(_draft(external_id="1790000000000000000", reply="")) is None
    assert x.reply_url(_draft(external_id="draft:5", reply=link)) is None


def test_discord_posts_ping_nobody():
    sent = []

    class Http:
        def request(self, method, url, body=None, headers=None):
            sent.append(json.loads(body))
            return 200, b'{"id": "7"}'

    body = "Thanks @everyone and @here, <@&123> and <@456>: 0.2.0 is out."
    env = {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/token"}
    result = DiscordPublisher().publish(_draft(platform="discord", body=body), env, Http())
    assert result.external_id == "7"
    assert sent == [{"content": body, "allowed_mentions": {"parse": []}}]


def test_telegram_render_fits_after_escaping():
    url = "https://pypi.org/project/demo/1.0/"
    for notes in ("- map<K, V> -> Vec<T> & more\n" * 300, "&" * 9000, "plain notes\n" * 20):
        release = Release("demo", "1.0", "A demo", url, None, {}, False, notes=notes)
        telegram = TelegramPublisher()
        rendered = telegram.render(release)
        telegram.validate(rendered.title, rendered.body)  # raises when the escaped text is over 4096
        assert len(html_text(rendered.body)) <= 4096 and rendered.body.endswith(url)
    assert "plain notes" in rendered.body


def test_telegram_counts_a_200_as_posted_whatever_the_body():
    env = {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "-100"}
    for body in (b"\xff\xfe", b"[]", b'{"result": 5}', b'"ok"', b"not json", b'{"result": {"message_id": 9}}'):

        class Http:
            def request(self, method, url, data=None, headers=None, body=body):
                return 200, body

        result = TelegramPublisher().publish(_draft(platform="telegram", body="0.2.0 is out."), env, Http())
        assert result.detail == "Posted to Telegram"
    assert result.external_id == "9"


def test_x_counts_a_201_as_posted_whatever_the_body():
    env = {"X_API_KEY": "k", "X_API_SECRET": "s", "X_ACCESS_TOKEN": "t", "X_ACCESS_TOKEN_SECRET": "ts"}
    for body in (b"\xff\xfe", b"[]", b'{"data": 5}', b'"ok"', b"not json", b'{"data": {"id": "9"}}'):

        class Http:
            def request(self, method, url, data=None, headers=None, body=body):
                return 201, body

        result = TwitterPublisher("api").publish(_draft(body="0.2.0 is out."), env, Http())
        assert result.detail == "Posted to Twitter / X"
    assert result.external_id == "9"


def test_a_missing_secret_or_a_refused_post_is_not_a_length_error():
    from argus_herald.platforms import LengthError, PublishConfigError, PublishRejected

    class Http:
        def request(self, method, url, data=None, headers=None):
            return 429, b"slow down"

    draft = _draft(platform="discord", body="0.2.0 is out.")
    with pytest.raises(PublishConfigError) as missing:
        DiscordPublisher().publish(draft, {}, Http())
    assert not isinstance(missing.value, LengthError)
    env = {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/token"}
    with pytest.raises(PublishRejected) as refused:
        DiscordPublisher().publish(draft, env, Http())
    assert refused.value.status == 429 and not isinstance(refused.value, LengthError)
    with pytest.raises(LengthError):
        DiscordPublisher().publish(_draft(platform="discord", body="x" * 2001), env, Http())


def test_a_plugin_without_a_usable_id_is_skipped(monkeypatch):
    from argus_herald import platforms

    class Entry:
        def __init__(self, name, obj):
            self.name = name
            self.obj = obj

        def load(self):
            return self.obj

    class NoId:
        pass

    class Forum(PatreonPublisher):
        id = "forum"

    def no_id_factory():
        return NoId()

    extras = [
        Entry("module-like", json),
        Entry("factory", no_id_factory),
        Entry("numeric", type("Numeric", (), {"id": 5})),
        Entry("good", Forum),
    ]
    monkeypatch.setattr(platforms, "entry_points", lambda group: extras)
    ids = [publisher.id for publisher in platforms.load_publishers()]
    assert ids == ["twitter", "discord", "telegram", "patreon", "forum"]
