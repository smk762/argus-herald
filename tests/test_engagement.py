import json
from datetime import date, datetime, timedelta, timezone

import pytest

from pip_release.cli import main
from pip_release.engagement import (
    bluesky_ref,
    collect_posts,
    collect_site,
    due_posts,
    import_x_analytics,
    mastodon_ref,
    record_x_week,
)
from pip_release.metrics import Metrics
from pip_release.schedule import suggest

T0 = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
DID = "did:plc:abc123"
BSKY_POST = "https://bsky.app/profile/dragonhound.dev/post/3kabc"
TOOT = "https://mapstodon.space/@dragonhound/113000000000000001"
X_POST = "https://x.com/dragonhound3/status/1990000000000000001"


class Fake:
    def __init__(self, likes=3):
        self.likes = likes
        self.calls = []
        self.posted = []

    def request(self, method, url, body=None, headers=None):
        self.calls.append(url)
        j = lambda data: (200, json.dumps(data).encode())  # noqa: E731
        if "resolveHandle" in url:
            return j({"did": DID})
        if "getPosts" in url:
            return j({"posts": [{"uri": f"at://{DID}/app.bsky.feed.post/3kabc", "likeCount": self.likes,
                                 "repostCount": 1, "replyCount": 2, "quoteCount": 0}]})
        if "/api/v1/statuses/113000000000000001" in url:
            return j({"favourites_count": 5, "reblogs_count": 2, "replies_count": 1})
        if "/api/v1/statuses/" in url:
            return 404, b"{}"
        if "graphql" in url:
            self.posted.append(json.loads(body))
            return j({"data": {"viewer": {"accounts": [{
                "paths": [{"count": 40, "sum": {"visits": 30}, "dimensions": {"date": "2026-11-20", "requestPath": "/updates/"}},
                          {"count": 5, "sum": {"visits": 5}, "dimensions": {"date": "2026-11-21", "requestPath": "/"}}],
                "referrers": [{"count": 20, "sum": {"visits": 18}, "dimensions": {"date": "2026-11-20", "refererHost": "bsky.app"}},
                              {"count": 25, "sum": {"visits": 17}, "dimensions": {"date": "2026-11-20", "refererHost": ""}}],
            }]}}})
        return 404, b"{}"


def _metrics(tmp_path):
    return Metrics(tmp_path / "m.sqlite")


def _broadcast(metrics, platform, url, at=T0):
    metrics.upsert("events", [{"kind": "broadcast", "source": platform, "project": "", "version": "",
                               "at": at.isoformat(), "url": url, "detail": "{}"}])


def test_post_refs():
    assert bluesky_ref(BSKY_POST) == ("dragonhound.dev", "3kabc")
    assert bluesky_ref(f"at://{DID}/app.bsky.feed.post/3kabc") == (DID, "3kabc")
    assert bluesky_ref("https://evil.example/profile/x/post/1") is None
    assert mastodon_ref(TOOT) == ("https://mapstodon.space", "113000000000000001")
    assert mastodon_ref("https://mapstodon.space/users/dragonhound/statuses/42") == ("https://mapstodon.space", "42")
    assert mastodon_ref("http://mapstodon.space/@dragonhound/1") is None


def test_posts_are_sampled_once_per_checkpoint(tmp_path):
    metrics = _metrics(tmp_path)
    _broadcast(metrics, "bluesky", BSKY_POST)
    _broadcast(metrics, "mastodon", TOOT)
    _broadcast(metrics, "bluesky", "draft:7")  # published without a URL: nothing to fetch
    http = Fake()

    assert collect_posts(metrics, http, T0 + timedelta(minutes=30)).lines == []
    assert http.calls == []

    report = collect_posts(metrics, http, T0 + timedelta(hours=1, minutes=5))
    assert report.errors == [] and len(report.lines) == 2
    rows = metrics.conn.execute("SELECT platform, likes, reposts, replies FROM post_metrics ORDER BY platform").fetchall()
    assert [tuple(r) for r in rows] == [("bluesky", 3, 1, 2), ("mastodon", 5, 2, 1)]

    calls = len(http.calls)
    collect_posts(metrics, http, T0 + timedelta(hours=5))  # still between +1h and +24h
    assert len(http.calls) == calls

    http.likes = 9
    collect_posts(metrics, http, T0 + timedelta(days=1, minutes=1))
    collect_posts(metrics, http, T0 + timedelta(days=3, minutes=1))
    collect_posts(metrics, http, T0 + timedelta(days=9))  # watcher was off at +7d: one late sample
    collect_posts(metrics, http, T0 + timedelta(days=30))
    assert metrics.count("post_metrics", platform="bluesky") == 4
    assert due_posts(metrics.conn, ["bluesky", "mastodon"], T0 + timedelta(days=60)) == []


def test_deleted_posts_are_skipped_not_errors(tmp_path):
    metrics = _metrics(tmp_path)
    _broadcast(metrics, "mastodon", "https://mapstodon.space/@dragonhound/999")
    report = collect_posts(metrics, Fake(), T0 + timedelta(hours=2))
    assert report.errors == [] and "not found" in report.skipped[0]


X_POSTS_CSV = (
    "﻿Post id,Date,Post text,Post Link,Impressions,Likes,Engagements,Bookmarks,Shares,New follows,Replies,"
    "Reposts,Profile visits,Detail Expands,URL Clicks\n"
    '1990000000000000001,"Mon, Nov 23, 2026",New release,https://x.com/dragonhound3/status/1990000000000000001,'
    '"1,204",14,60,2,0,3,1,4,9,12,7\n'
    '1990000000000000002,"Tue, Nov 24, 2026",An ordinary post,https://x.com/dragonhound3/status/1990000000000000002,'
    "300,2,10,0,0,0,0,0,1,3,0\n"
    ",,,,oops,,,,,,,,,,\n"
)
X_DAYS_CSV = (
    "Date,Impressions,Likes,Engagements,Bookmarks,Shares,New follows,Unfollows,Replies,Reposts,Profile visits\n"
    '"Mon, Nov 23, 2026",2100,20,95,2,1,4,1,3,5,12\n'
    '"Tue, Nov 24, 2026",800,6,30,0,0,1,0,1,0,4\n'
)


def test_x_analytics_import_matches_broadcasts(tmp_path):
    metrics = _metrics(tmp_path)
    _broadcast(metrics, "twitter", X_POST)
    result = import_x_analytics(metrics, X_POSTS_CSV, "dragonhound3", now=T0 + timedelta(days=2))
    assert (result.posts, result.matched) == (2, 1) and len(result.skipped) == 1
    row = metrics.conn.execute("SELECT * FROM post_metrics WHERE external_id = ?", (X_POST,)).fetchone()
    assert (row["platform"], row["impressions"], row["likes"], row["reposts"], row["link_clicks"],
            row["profile_visits"], row["source"]) == ("twitter", 1204, 14, 4, 7, 9, "x-analytics")
    assert metrics.count("post_metrics", external_id="https://x.com/dragonhound3/status/1990000000000000002") == 1

    days = import_x_analytics(metrics, X_DAYS_CSV, "dragonhound3")
    assert days.days == 2
    row = metrics.conn.execute("SELECT * FROM account_engagement WHERE start_date = '2026-11-23'").fetchone()
    assert (row["impressions"], row["engagements"], row["new_follows"], row["profile_visits"]) == (2100, 95, 4, 12)

    with pytest.raises(ValueError):
        import_x_analytics(metrics, "name,score\na,1\n", "dragonhound3")


def test_x_week_and_slot_suggestions(tmp_path):
    metrics = _metrics(tmp_path)
    record_x_week(metrics, "dragonhound3", date(2026, 11, 23), date(2026, 11, 29), impressions=9000, link_clicks=40)
    row = metrics.conn.execute("SELECT * FROM account_engagement").fetchone()
    assert (row["end_date"], row["impressions"], row["link_clicks"], row["source"]) == ("2026-11-29", 9000, 40, "manual")

    _broadcast(metrics, "twitter", X_POST)
    import_x_analytics(metrics, X_POSTS_CSV, "dragonhound3")
    metrics.close()
    text = suggest(tmp_path / "m.sqlite")
    assert "twitter: 1 posts" in text and "Mon 08-12 UTC" in text


def test_site_traffic_replaces_the_window(tmp_path):
    metrics = _metrics(tmp_path)
    metrics.upsert("site_traffic", [{"date": "2026-11-20", "path": "/old/", "pageviews": 1, "visits": 1},
                                    {"date": "2026-11-01", "path": "/", "pageviews": 7, "visits": 7}])
    http = Fake()
    env = {"CF_API_TOKEN": "t", "CF_ACCOUNT_ID": "a", "CF_SITE_TAG": "s"}
    detail = collect_site(metrics, http, env, today=date(2026, 11, 22))
    assert "45 page views" in detail
    paths = {(r["date"], r["path"]): r["pageviews"] for r in metrics.conn.execute("SELECT * FROM site_traffic")}
    assert paths == {("2026-11-20", "/updates/"): 40, ("2026-11-21", "/"): 5, ("2026-11-01", "/"): 7}
    refs = {r["referrer"] for r in metrics.conn.execute("SELECT * FROM site_referrers")}
    assert refs == {"bsky.app", "(direct)"}
    flt = http.posted[0]["variables"]["filter"]["AND"]
    assert {"date_geq": "2026-11-16"} in flt and {"siteTag": "s"} in flt


def test_site_traffic_reports_graphql_errors(tmp_path):
    class Errors(Fake):
        def request(self, method, url, body=None, headers=None):
            return 200, json.dumps({"data": None, "errors": [{"message": "not authorized"}]}).encode()

    with pytest.raises(ValueError, match="not authorized"):
        collect_site(_metrics(tmp_path), Errors(), {"CF_API_TOKEN": "t", "CF_ACCOUNT_ID": "a", "CF_SITE_TAG": "s"})


def test_import_cli(tmp_path, capsys):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("pypi: {projects: []}\ndestinations:\n  twitter: {enabled: true, username: dragonhound3}\n",
                   encoding="utf-8")
    base = ["--config", str(cfg), "--env", str(tmp_path / "no.env"), "--store", str(tmp_path / "s.sqlite")]
    csv_file = tmp_path / "x.csv"
    csv_file.write_text(X_POSTS_CSV, encoding="utf-8")
    assert main([*base, "import", "x-analytics", str(csv_file)]) == 0
    assert "Imported 2 X posts (0 matched" in capsys.readouterr().out
    assert main([*base, "import", "x-week", "--end", "2026-11-29", "--impressions", "9000"]) == 0
    assert "2026-11-23 to 2026-11-29: 9000 impressions" in capsys.readouterr().out
    assert main([*base, "import", "x-week"]) != 0
    assert main([*base, "import", "x-account"]) != 0
    assert main([*base, "import", "x-analytics"]) != 0
    assert main([*base, "platforms"]) == 0
    assert "Site traffic   missing CF_API_TOKEN" in capsys.readouterr().out
