import json
from datetime import UTC, date, datetime, timedelta

from starlette.testclient import TestClient

from argus_herald import dashboard as d
from argus_herald.charts import series_panel, stacked_area
from argus_herald.config import load_config
from argus_herald.metrics import Metrics
from argus_herald.ui.app import create_app

END = date(2026, 12, 20)


def _metrics(tmp_path):
    return Metrics(tmp_path / "metrics.sqlite")


def _broadcast(m, platform, at, url, post, **detail):
    m.upsert(
        "events",
        [
            {
                "kind": "broadcast",
                "source": platform,
                "project": detail.pop("project", "argus-atlas"),
                "version": "",
                "at": at.isoformat(),
                "url": url,
                "detail": json.dumps(
                    {
                        "kind": "release",
                        "subject": post,
                        "text": "t",
                        "post": post,
                        "media": 0,
                        "drafted_by": "template",
                        "logged": False,
                        **detail,
                    }
                ),
            }
        ],
    )


def _at(day: date, hour=12):
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def test_referrer_groups_and_platform_slots():
    assert d.referrer_group("t.co") == "twitter" and d.referrer_group("bsky.app") == "bluesky"
    assert d.referrer_group("fosstodon.org") == "mastodon" and d.referrer_group("old.reddit.com") == "reddit"
    assert d.referrer_group("news.ycombinator.com") == "hn" and d.referrer_group("www.google.com") == "search"
    assert d.referrer_group("example.dev", ("example.dev",)) == "site" and d.referrer_group("github.com") == "other"
    assert d.platform_slot("x") == d.platform_slot("twitter") == 1
    assert d.platform_slot("patreon") == 8 and d.platform_label("patreon") == "Patreon"


def test_timeline_marks_uncollected_days_and_derives_new_stars(tmp_path):
    m = _metrics(tmp_path)
    m.upsert(
        "pypi_downloads",
        [{"package": "argus-atlas", "date": (END - timedelta(days=i)).isoformat(), "downloads": 10} for i in range(5)],
    )
    m.upsert(
        "gh_star_counts",
        [
            {"repo": "argus-atlas", "date": (END - timedelta(days=2 - i)).isoformat(), "stars": s}
            for i, s in enumerate((10, 12, 12))
        ],
    )
    conn = d.connect(m.path)
    f = d.Filters(days=7, end=END)
    series = d.timeline(conn, f)
    assert series["downloads"] == [None, None, 10, 10, 10, 10, 10]
    assert series["stars"] == [None, None, None, None, 0, 2, 0]
    assert series["visits"] == [None] * 7
    assert d.rolling_mean([1, 2, 3, None, 5], window=3)[:3] == [None, None, 2]
    assert d.projects(conn) == ["argus-atlas"]


def test_event_window_compares_with_the_baseline_median(tmp_path):
    m = _metrics(tmp_path)
    post_day = END - timedelta(days=5)
    rows = []
    for i in range(40):
        day = END - timedelta(days=i)
        bump = 30 if post_day <= day <= post_day + timedelta(days=2) else 0
        rows.append({"package": "argus-atlas", "date": day.isoformat(), "downloads": 10 + bump})
    m.upsert("pypi_downloads", rows)
    _broadcast(m, "bluesky", _at(post_day), "https://bsky.app/profile/a.b/post/1", "one")
    _broadcast(m, "twitter", _at(post_day - timedelta(days=20)), "https://x.com/a/status/2", "two")
    report = d.broadcast_report(d.connect(m.path), d.Filters(days=30, end=END))
    one = next(b for b in report if b.post == "one")
    assert one.deltas["downloads"] == 30 and one.baselines["downloads"] == 10
    assert one.deltas["views"] is None  # no traffic collected
    assert not one.overlap


def test_overlaps_and_engagement_checkpoints(tmp_path):
    m = _metrics(tmp_path)
    t0 = _at(END - timedelta(days=10))
    _broadcast(m, "twitter", t0, "https://x.com/a/status/1", "a")
    _broadcast(m, "discord", t0 + timedelta(minutes=30), "draft:5", "a")  # same post: not an overlap
    _broadcast(m, "bluesky", t0 + timedelta(hours=40), "https://bsky.app/profile/a.b/post/9", "b", kind="digest")
    rows = [
        ("https://x.com/a/status/1", "twitter", t0 + timedelta(days=8), 20, "x-analytics"),
        ("https://bsky.app/profile/a.b/post/9", "bluesky", t0 + timedelta(hours=41), 1, "api"),
        ("https://bsky.app/profile/a.b/post/9", "bluesky", t0 + timedelta(hours=65), 5, "api"),
        ("https://bsky.app/profile/a.b/post/9", "bluesky", t0 + timedelta(hours=40 + 169), 9, "api"),
    ]
    m.upsert(
        "post_metrics",
        [
            {
                "platform": p,
                "external_id": u,
                "fetched_at": at.isoformat(),
                "likes": likes,
                "reposts": 0,
                "replies": 0,
                "quotes": 0,
                "impressions": 1000 if p == "twitter" else None,
                "engagements": None,
                "link_clicks": None,
                "profile_visits": None,
                "source": src,
            }
            for u, p, at, likes, src in rows
        ],
    )
    report = {b.platform: b for b in d.broadcast_report(d.connect(m.path), d.Filters(days=30, end=END))}
    assert report["twitter"].overlap and report["bluesky"].overlap and report["discord"].overlap
    # An X import 8 days on is the 7-day figure, not the 24h one.
    assert (report["twitter"].engagement_24h, report["twitter"].engagement_7d, report["twitter"].impressions) == (
        None,
        20,
        1000,
    )
    assert (report["bluesky"].engagement_24h, report["bluesky"].engagement_7d) == (5, 9)
    only = d.filter_report(list(report.values()), platform="bluesky", kind="digest", media="no")
    assert [b.platform for b in only] == ["bluesky"]


def test_platform_summary_weeks_and_follower_change(tmp_path):
    m = _metrics(tmp_path)
    monday = date(2026, 12, 14)
    _broadcast(m, "twitter", _at(monday + timedelta(days=2)), "https://x.com/a/status/1", "a")
    m.upsert(
        "account_metrics",
        [
            {
                "platform": "x",
                "account": "a",
                "date": (monday + timedelta(days=i)).isoformat(),
                "followers": 100 + i,
                "following": None,
                "posts": None,
                "source": "manual",
            }
            for i in range(-3, 7)
        ],
    )
    conn = d.connect(m.path)
    f = d.Filters(days=14, end=END)
    rows = d.platform_summary(conn, d.broadcast_report(conn, f), f)
    week = next(r for r in rows if r["week"] == monday and r["platform"] == "twitter")
    assert week["posts"] == 1 and week["followers"] == 7  # 106 on Sunday against 99 the Sunday before
    sunday_weeks = d.platform_summary(conn, d.broadcast_report(conn, f), d.Filters(days=14, end=END, week_start=6))
    assert {r["week"].weekday() for r in sunday_weeks} == {6}


def test_charts_escape_text_and_never_draw_nan(tmp_path):
    days = [END - timedelta(days=i) for i in range(4, -1, -1)]
    post = d.Broadcast(
        platform="bluesky",
        at=_at(days[2]),
        url="javascript:alert(1)",
        project="",
        kind="note",
        subject="<script>x</script>",
        text="t",
        logged=False,
        drafted_by="template",
        post="p",
        media=0,
    )
    svg = str(series_panel("dl", "Downloads", days, [None, 0, 3, 5, 2], [None, None, 1, 2, 3], [], [post], log=True))
    assert "NaN" not in svg and "<script>" not in svg and "javascript:" not in svg
    assert "Not collected yet" in str(series_panel("e", "Empty", days, [None] * 5, None, [], []))
    area = str(stacked_area([d_.isoformat() for d_ in days], [("twitter", "X", 1, [1, 2, 3, 4, 5])]))
    assert "NaN" not in area and 'class="layer s1"' in area


def _app(tmp_path, token=""):
    import os

    path = tmp_path / "config.yaml"
    path.write_text(
        "pypi: {projects: [argus-atlas]}\ndestinations:\n  twitter: {enabled: true, username: a}\n", encoding="utf-8"
    )
    config = load_config(path)
    config.store_path = tmp_path / "state.sqlite"
    env = {
        "ARGUS_HERALD_UI_SECRET": "s",
        "ARGUS_HERALD_OPSEC_DENYLIST": os.environ["ARGUS_HERALD_OPSEC_DENYLIST"],
        "AI_AUDIT_OPSEC_DENYLIST": os.environ["AI_AUDIT_OPSEC_DENYLIST"],
    }
    if token:
        env["ARGUS_HERALD_UI_TOKEN"] = token
    return create_app(config, env), config


def test_dashboard_routes(tmp_path):
    app, config = _app(tmp_path)
    client = TestClient(app)
    empty = client.get("/dashboard")
    assert empty.status_code == 200 and "No metrics yet" in empty.text
    m = Metrics(tmp_path / "metrics.sqlite")
    today = datetime.now(UTC).date()
    m.upsert(
        "pypi_downloads",
        [
            {"package": "argus-atlas", "date": (today - timedelta(days=i)).isoformat(), "downloads": 5}
            for i in range(30)
        ],
    )
    _broadcast(m, "twitter", _at(today - timedelta(days=3)), "https://x.com/a/status/1", "<b>post</b>")
    page = client.get("/dashboard?days=30&scale=log&project=nope&days=bad")
    assert page.status_code == 200 and "PyPI downloads" in page.text and "&lt;b&gt;post&lt;/b&gt;" in page.text
    csv = client.get("/dashboard/broadcasts.csv?days=30")
    assert csv.headers["content-type"].startswith("text/csv") and "x.com/a/status/1" in csv.text
    assert client.get("/dashboard/timeline.csv").text.startswith("date,downloads")
    assert client.get("/dashboard/secrets.csv").status_code == 404

    locked, _ = _app(tmp_path, token="t0ken")
    guarded = TestClient(locked, follow_redirects=False)
    assert guarded.get("/dashboard").headers["location"] == "/login"
    assert guarded.get("/dashboard/broadcasts.csv").headers["location"] == "/login"
