import json
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from argus_herald.cli import main
from argus_herald.engagement import (
    bluesky_ref,
    collect_posts,
    collect_site,
    due_posts,
    import_x_analytics,
    mastodon_ref,
    record_x_week,
)
from argus_herald.metrics import Metrics
from argus_herald.schedule import suggest

T0 = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
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
            return j(
                {
                    "posts": [
                        {
                            "uri": f"at://{DID}/app.bsky.feed.post/3kabc",
                            "likeCount": self.likes,
                            "repostCount": 1,
                            "replyCount": 2,
                            "quoteCount": 0,
                        }
                    ]
                }
            )
        if "/api/v1/statuses/113000000000000001" in url:
            return j({"favourites_count": 5, "reblogs_count": 2, "replies_count": 1})
        if "/api/v1/statuses/" in url:
            return 404, b"{}"
        if "graphql" in url:
            self.posted.append(json.loads(body))
            return j(
                {
                    "data": {
                        "viewer": {
                            "accounts": [
                                {
                                    "paths": [
                                        {
                                            "count": 40,
                                            "sum": {"visits": 30},
                                            "dimensions": {
                                                "date": "2026-11-20",
                                                "requestHost": "Example.org",
                                                "requestPath": "/updates/",
                                            },
                                        },
                                        {
                                            "count": 3,
                                            "sum": {"visits": 3},
                                            "dimensions": {
                                                "date": "2026-11-21",
                                                "requestHost": "example.org",
                                                "requestPath": "/",
                                            },
                                        },
                                        {
                                            "count": 2,
                                            "sum": {"visits": 2},
                                            "dimensions": {
                                                "date": "2026-11-21",
                                                "requestHost": "docs.example.org",
                                                "requestPath": "/",
                                            },
                                        },
                                    ],
                                    "referrers": [
                                        {
                                            "count": 20,
                                            "sum": {"visits": 18},
                                            "dimensions": {"date": "2026-11-20", "refererHost": "bsky.app"},
                                        },
                                        {
                                            "count": 25,
                                            "sum": {"visits": 17},
                                            "dimensions": {"date": "2026-11-20", "refererHost": ""},
                                        },
                                    ],
                                }
                            ]
                        }
                    }
                }
            )
        return 404, b"{}"


def _metrics(tmp_path):
    return Metrics(tmp_path / "m.sqlite")


def _broadcast(metrics, platform, url, at=T0):
    metrics.upsert(
        "events",
        [
            {
                "kind": "broadcast",
                "source": platform,
                "project": "",
                "version": "",
                "at": at.isoformat(),
                "url": url,
                "detail": "{}",
            }
        ],
    )


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
    rows = metrics.conn.execute(
        "SELECT platform, likes, reposts, replies FROM post_metrics ORDER BY platform"
    ).fetchall()
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
    assert (
        row["platform"],
        row["impressions"],
        row["likes"],
        row["reposts"],
        row["link_clicks"],
        row["profile_visits"],
        row["source"],
    ) == ("twitter", 1204, 14, 4, 7, 9, "x-analytics")
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
    assert (row["end_date"], row["impressions"], row["link_clicks"], row["source"]) == (
        "2026-11-29",
        9000,
        40,
        "manual",
    )

    _broadcast(metrics, "twitter", X_POST)
    import_x_analytics(metrics, X_POSTS_CSV, "dragonhound3")
    metrics.close()
    text = suggest(tmp_path / "m.sqlite")
    assert "twitter: 1 posts" in text and "Mon 08-12 UTC" in text


def test_site_traffic_replaces_the_window(tmp_path):
    metrics = _metrics(tmp_path)
    metrics.upsert(
        "site_traffic",
        [
            {"date": "2026-11-20", "path": "/old/", "pageviews": 1, "visits": 1},
            {"date": "2026-11-01", "path": "/", "pageviews": 7, "visits": 7},
        ],
    )
    http = Fake()
    env = {"CF_API_TOKEN": "t", "CF_ACCOUNT_ID": "a", "CF_SITE_TAG": "s"}
    detail = collect_site(metrics, http, env, today=date(2026, 11, 22))
    assert "45 page views" in detail
    assert "2 hosts, 3 paths" in detail
    paths = {
        (r["date"], r["host"], r["path"]): r["pageviews"] for r in metrics.conn.execute("SELECT * FROM site_traffic")
    }
    assert paths == {
        ("2026-11-20", "example.org", "/updates/"): 40,
        ("2026-11-21", "example.org", "/"): 3,  # the same path on two hosts stays two rows
        ("2026-11-21", "docs.example.org", "/"): 2,
        ("2026-11-01", "", "/"): 7,
    }
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
    cfg.write_text(
        "pypi: {projects: []}\ndestinations:\n  twitter: {enabled: true, username: dragonhound3}\n", encoding="utf-8"
    )
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


def test_an_unresolvable_handle_only_costs_its_own_posts(tmp_path):
    class Renamed(Fake):
        def request(self, method, url, body=None, headers=None):
            if "resolveHandle" in url and "gone.example" in url:
                self.calls.append(url)
                return 400, b'{"error":"InvalidRequest","message":"Unable to resolve handle"}'
            if "resolveHandle" in url and "odd.example" in url:
                self.calls.append(url)
                return 200, b"{}"  # an answer with no did
            return super().request(method, url, body, headers)

    metrics = _metrics(tmp_path)
    _broadcast(metrics, "bluesky", "https://bsky.app/profile/gone.example/post/3kaaa")
    _broadcast(metrics, "bluesky", "https://bsky.app/profile/gone.example/post/3kbbb")
    _broadcast(metrics, "bluesky", "https://bsky.app/profile/odd.example/post/3kccc")
    _broadcast(metrics, "bluesky", BSKY_POST)
    http = Renamed()
    report = collect_posts(metrics, http, T0 + timedelta(hours=2))
    assert metrics.count("post_metrics", platform="bluesky", external_id=BSKY_POST) == 1
    assert sum("can't resolve handle gone.example" in line for line in report.skipped + report.errors) == 2
    assert sum("gone.example" in url for url in http.calls) == 1  # asked once, not per post


def test_site_backfill_leaves_later_days_alone(tmp_path):
    metrics = _metrics(tmp_path)
    later = [{"date": "2026-11-25", "path": "/later/", "pageviews": 3, "visits": 3}]
    metrics.upsert("site_traffic", later)
    metrics.upsert("site_referrers", [{"date": "2026-11-25", "referrer": "t.co", "pageviews": 2, "visits": 2}])
    env = {"CF_API_TOKEN": "t", "CF_ACCOUNT_ID": "a", "CF_SITE_TAG": "s"}
    collect_site(metrics, Fake(), env, today=date(2026, 11, 22))  # a re-run for an earlier week
    paths = {(r["date"], r["path"]) for r in metrics.conn.execute("SELECT * FROM site_traffic")}
    assert ("2026-11-25", "/later/") in paths and ("2026-11-20", "/updates/") in paths
    assert metrics.count("site_referrers", date="2026-11-25") == 1


@pytest.mark.parametrize("blank", ["CF_API_TOKEN", "CF_ACCOUNT_ID", "CF_SITE_TAG"])
def test_site_traffic_needs_all_three_settings(tmp_path, blank):
    class Empty(Fake):
        def request(self, method, url, body=None, headers=None):
            self.calls.append(url)
            empty = {"data": {"viewer": {"accounts": [{"paths": [], "referrers": []}]}}}
            return 200, json.dumps(empty).encode()

    metrics = _metrics(tmp_path)
    metrics.upsert("site_traffic", [{"date": "2026-11-20", "path": "/kept/", "pageviews": 1, "visits": 1}])
    env = {"CF_API_TOKEN": "t", "CF_ACCOUNT_ID": "a", "CF_SITE_TAG": "s", blank: "  "}
    http = Empty()
    with pytest.raises(ValueError, match=blank):
        collect_site(metrics, http, env, today=date(2026, 11, 22))
    assert http.calls == [] and metrics.count("site_traffic") == 1


def test_slot_suggestions_bucket_by_utc(tmp_path):
    metrics = _metrics(tmp_path)
    # Monday 23:30 at -05:00 is Tuesday 04:30 UTC.
    _broadcast(metrics, "twitter", X_POST, at=datetime(2026, 11, 23, 23, 30, tzinfo=timezone(timedelta(hours=-5))))
    import_x_analytics(metrics, X_POSTS_CSV, "dragonhound3")
    metrics.close()
    text = suggest(tmp_path / "m.sqlite")
    assert "Tue 04-08 UTC" in text and "Mon" not in text


def test_mastodon_ref_keeps_the_port_and_ipv6_brackets():
    from argus_herald.engagement import mastodon_ref

    assert mastodon_ref("https://social.example:8443/@user/123") == ("https://social.example:8443", "123")
    assert mastodon_ref("https://[::1]:8443/@user/5") == ("https://[::1]:8443", "5")
    assert mastodon_ref("https://name:pw@social.example/@user/123") == ("https://social.example", "123")
    assert mastodon_ref("https://social.example:port/@user/123") is None


def test_a_number_cell_that_overflows_is_not_a_number():
    from argus_herald.engagement import _number

    for bad in ("inf", "-Infinity", "1e999", "nan", "abc"):
        with pytest.raises(ValueError, match="not a number"):
            _number(bad)
    assert _number("9007199254740993") == 9007199254740993  # exact past 2**53
    assert _number("1,234") == 1234 and _number("12.0") == 12 and _number("1e3") == 1000 and _number("-") is None


def test_site_traffic_table_gains_a_host_column(tmp_path):
    """A metrics file from before hosts were recorded keeps its rows, and can then hold two hosts."""
    import sqlite3

    path = tmp_path / "metrics.sqlite"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE site_traffic (date TEXT NOT NULL, path TEXT NOT NULL, pageviews INTEGER NOT NULL, "
        "visits INTEGER NOT NULL, PRIMARY KEY (date, path))"
    )
    old.execute("INSERT INTO site_traffic VALUES ('2026-11-01', '/', 7, 6)")
    old.commit()
    old.close()
    metrics = Metrics(path)
    rows = [tuple(r) for r in metrics.conn.execute("SELECT date, host, path, pageviews, visits FROM site_traffic")]
    assert rows == [("2026-11-01", "", "/", 7, 6)]
    day = {"date": "2026-11-02", "path": "/", "pageviews": 1, "visits": 1}
    metrics.upsert("site_traffic", [{**day, "host": "example.org"}, {**day, "host": "docs.example.org"}])
    assert metrics.count("site_traffic") == 3
    metrics.close()
    assert Metrics(path).count("site_traffic") == 3  # opening again changes nothing


def test_a_failed_getposts_batch_only_costs_its_own_posts(tmp_path):
    from urllib.parse import parse_qs, urlsplit

    class Flaky(Fake):
        def request(self, method, url, body=None, headers=None):
            if "getPosts" in url:
                self.calls.append(url)
                if "3kzzbad" in url:
                    return 502, b"bad gateway"
                uris = parse_qs(urlsplit(url).query)["uris"]
                posts = [{"uri": u, "likeCount": 1, "repostCount": 0, "replyCount": 0, "quoteCount": 0} for u in uris]
                return 200, json.dumps({"posts": posts}).encode()
            return super().request(method, url, body, headers)

    metrics = _metrics(tmp_path)
    good = [f"https://bsky.app/profile/{DID}/post/3kz{n:02d}" for n in range(25)]  # one full batch
    for url in good:
        _broadcast(metrics, "bluesky", url)
    # Posts are batched in URL order, so this one sorts last and is alone in the second batch.
    _broadcast(metrics, "bluesky", f"https://bsky.app/profile/{DID}/post/3kzzbad")
    http = Flaky()
    report = collect_posts(metrics, http, T0 + timedelta(hours=2))
    assert sum("getPosts" in url for url in http.calls) == 2
    assert report.errors == []
    assert [url for url in good if metrics.count("post_metrics", platform="bluesky", external_id=url) != 1] == []
    assert len(report.skipped) == 1
    assert "can't fetch" in report.skipped[0] and "3kzzbad" in report.skipped[0]
    assert (
        metrics.count("post_metrics", platform="bluesky", external_id=f"https://bsky.app/profile/{DID}/post/3kzzbad")
        == 0
    )


def test_a_getposts_answer_that_isnt_an_object_is_a_problem_not_a_crash(tmp_path):
    class Listy(Fake):
        def request(self, method, url, body=None, headers=None):
            if "getPosts" in url:
                return 200, b"[]"
            return super().request(method, url, body, headers)

    metrics = _metrics(tmp_path)
    _broadcast(metrics, "bluesky", BSKY_POST)
    report = collect_posts(metrics, Listy(), T0 + timedelta(hours=2))
    assert report.errors == [] and "not found" in report.skipped[0]


def test_a_handle_link_and_a_did_link_to_one_post_both_get_a_sample(tmp_path):
    metrics = _metrics(tmp_path)
    did_link = f"https://bsky.app/profile/{DID}/post/3kabc"
    _broadcast(metrics, "bluesky", BSKY_POST)
    _broadcast(metrics, "bluesky", did_link)
    report = collect_posts(metrics, Fake(), T0 + timedelta(hours=2))
    assert report.skipped == [] and report.errors == []
    assert metrics.count("post_metrics", external_id=BSKY_POST) == 1
    assert metrics.count("post_metrics", external_id=did_link) == 1


def test_a_malformed_host_is_not_a_post_ref():
    assert mastodon_ref("https://[::1/@a/1") is None
    assert mastodon_ref("https://[foo]/@a/1") is None
    assert bluesky_ref("https://[::1/profile/a/post/3k") is None


def test_slash_dates_take_one_order_per_export(tmp_path):
    metrics = _metrics(tmp_path)
    header = "Date,Impressions,Likes,Engagements\n"
    import_x_analytics(metrics, header + "03/04/2026,10,1,2\n03/05/2026,10,1,2\n", "dragonhound3")
    days = {r["start_date"] for r in metrics.conn.execute("SELECT start_date FROM account_engagement")}
    assert days == {"2026-03-04", "2026-03-05"}  # X writes month first

    # A first number over 12 shows this export is day-first, so 03/04 is 3 April, not 4 March.
    import_x_analytics(metrics, header + "03/04/2025,10,1,2\n25/04/2025,10,1,2\n", "dragonhound3")
    days = {r["start_date"] for r in metrics.conn.execute("SELECT start_date FROM account_engagement")}
    assert {"2025-04-03", "2025-04-25"} <= days and "2025-03-04" not in days

    with pytest.raises(ValueError, match="mixes"):
        import_x_analytics(metrics, header + "25/04/2025,10,1,2\n04/26/2025,10,1,2\n", "dragonhound3")


def test_site_rows_that_normalise_to_one_key_are_added_up(tmp_path):
    def row(count, visits, **dims):
        return {"count": count, "sum": {"visits": visits}, "dimensions": {"date": "2026-11-21", **dims}}

    class Colliding(Fake):
        def request(self, method, url, body=None, headers=None):
            if "graphql" not in url:
                return super().request(method, url, body, headers)
            paths = [
                row(4, 3, requestHost="Example.org", requestPath="/"),
                row(2, 2, requestHost="example.org", requestPath="/"),
                row(1, 1, requestHost="example.org", requestPath=""),
            ]
            refs = [row(5, 4, refererHost=""), row(3, 3, refererHost=None)]
            data = {"data": {"viewer": {"accounts": [{"paths": paths, "referrers": refs}]}}}
            return 200, json.dumps(data).encode()

    metrics = _metrics(tmp_path)
    env = {"CF_API_TOKEN": "t", "CF_ACCOUNT_ID": "a", "CF_SITE_TAG": "s"}
    detail = collect_site(metrics, Colliding(), env, today=date(2026, 11, 22))
    traffic = [tuple(r) for r in metrics.conn.execute("SELECT host, path, pageviews, visits FROM site_traffic")]
    assert traffic == [("example.org", "/", 7, 6)]
    refs = [tuple(r) for r in metrics.conn.execute("SELECT referrer, pageviews, visits FROM site_referrers")]
    assert refs == [("(direct)", 8, 7)]
    assert "1 paths, 7 page views, 1 referrer rows" in detail
