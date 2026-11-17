import json

from argus_herald.config import Audience, Destination
from argus_herald.models import Draft
from argus_herald.platforms import DiscordPublisher, PatreonPublisher, TwitterPublisher


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
