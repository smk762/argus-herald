import itertools
from datetime import UTC, datetime, timedelta

import pytest

from argus_herald import clips, performance
from argus_herald.cli import main
from argus_herald.config import ConfigError, PerformanceSettings, load_config
from argus_herald.metrics import Metrics
from argus_herald.performance import NO_DATA, STRONG, TYPICAL, WEAK, clip_performance, rate
from argus_herald.store import Store

NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)
SETTINGS = PerformanceSettings()
_ids = itertools.count(1)


def published(store, platform, url, *, days_ago=1.0, clip=None):
    """A draft that went out `days_ago` before NOW, optionally carrying a clip."""
    draft_id, _ = store.add_draft(platform=platform, title="", body="b", link="l", post_key=f"post-{next(_ids)}")
    store.transition(draft_id, ("pending",), "published", external_id=url)
    at = (NOW - timedelta(days=days_ago)).isoformat()
    store.conn.execute("UPDATE drafts SET updated_at = ? WHERE id = ?", (at, draft_id))
    if clip:
        for _ in range(2):  # two media items from one clip are still one post
            store.add_media(
                draft_id, kind="video", path="v.mp4", name="v.mp4", alt="a", size=1, removed="", clip_id=clip
            )
    return draft_id


def logged(store, platform, url, *, days_ago=1.0, clip=None):
    at = (NOW - timedelta(days=days_ago)).isoformat()
    bid = store.log_broadcast(platform=platform, url=url, kind="demo", project="", subject="s", text="t", at=at)
    if clip:
        clips.link_broadcast(store, bid, clip)
    return bid


def sample(metrics, platform, url, *, hours_after=24.0, **counts):
    keys = ("likes", "reposts", "replies", "quotes", "impressions", "engagements", "link_clicks", "profile_visits")
    row = {"platform": platform, "external_id": url, "fetched_at": (NOW - timedelta(hours=hours_after)).isoformat()}
    row.update({k: counts.get(k) for k in keys})
    metrics.upsert("post_metrics", [{**row, "source": counts.get("source", "api")}])


def baseline(store, metrics, platform, values, *, days_ago=2.0, measure="impressions", host="x.com/a/status"):
    """Posts without a clip, one per value, each with a sample."""
    for value in values:
        url = f"https://{host}/{next(_ids)}"
        published(store, platform, url, days_ago=days_ago)
        sample(metrics, platform, url, **{measure: value})


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    metrics = Metrics(tmp_path / "metrics.sqlite")
    clips.add(store, "atlas-loop", demo="atlas", status="posted")
    yield store, metrics, tmp_path / "metrics.sqlite"
    metrics.close()
    store.close()


def perf(env, settings=SETTINGS):
    store, _metrics, path = env
    return clip_performance(store, path, settings, now=NOW)


# ---- the rating, over plain numbers --------------------------------------------------------


def test_every_rating_and_the_exact_boundaries():
    base = [100, 100, 100, 100, 100]
    assert rate([150], base, SETTINGS).label == STRONG  # exactly 1.5
    assert rate([149.9], base, SETTINGS).label == TYPICAL
    assert rate([100], base, SETTINGS).label == TYPICAL
    assert rate([50.1], base, SETTINGS).label == TYPICAL
    assert rate([50], base, SETTINGS).label == WEAK  # exactly 0.5
    assert rate([0], base, SETTINGS).label == WEAK
    found = rate([100, 300, 500], [10, 20, 200, 200, 300], SETTINGS)  # medians: 300 / 200
    assert (found.label, found.ratio, found.clip_median, found.baseline_median, found.baseline_posts) == (
        STRONG,
        1.5,
        300,
        200,
        5,
    )


def test_no_data_never_weak():
    few = rate([0], [100] * 4, SETTINGS)
    assert (few.label, few.why, few.ratio) == (NO_DATA, "4 baseline posts, needs 5", None)
    zero = rate([10], [0, 0, 0, 5, 9], SETTINGS)  # a zero median is no data, not a division error
    assert (zero.label, zero.why) == (NO_DATA, "baseline median is 0")
    unsampled = rate([], [100] * 9, SETTINGS)
    assert (unsampled.label, unsampled.why, unsampled.baseline_median) == (NO_DATA, "no sample yet", 100)
    custom = PerformanceSettings(strong=2.0, weak=0.8, min_posts=2, days=7)
    assert [rate([v], [100, 100], custom).label for v in (200, 199, 81, 80)] == [STRONG, TYPICAL, TYPICAL, WEAK]


# ---- the join ------------------------------------------------------------------------------


def test_clip_posted_twice_sums_its_posts_and_gets_one_rating(env):
    store, metrics, _ = env
    baseline(store, metrics, "twitter", [100, 100, 100, 100, 100, 100])
    # One post from a draft, one logged by hand under the other name for X: both are the X platform.
    published(store, "twitter", "https://x.com/a/status/901", clip="atlas-loop")
    sample(metrics, "twitter", "https://x.com/a/status/901", impressions=300, engagements=20, link_clicks=4)
    logged(store, "x", "https://x.com/a/status/902", clip="atlas-loop", days_ago=0.5)
    sample(metrics, "x", "https://x.com/a/status/902", impressions=200, engagements=10, link_clicks=None)

    found = perf(env)["atlas-loop"]
    assert list(found.platforms) == ["twitter"]
    x = found.platforms["twitter"]
    assert (x.posts, x.sampled, x.impressions, x.engagements, x.link_clicks, x.measure) == (
        2,
        2,
        500,
        30,
        4,
        "impressions",
    )
    # median 250 against a baseline of 8 posts (the clip's own two included): median 100
    assert (x.rating.label, x.rating.ratio, x.rating.baseline_median, x.rating.baseline_posts) == (STRONG, 2.5, 100, 8)
    assert found.summary() == "x strong"
    assert found.details() == [
        "x: strong 2.50x; 2 posts: 500 impressions, 30 engagements, 4 link clicks; "
        "median 250 impressions vs baseline median 100 over 8 posts"
    ]
    assert found.last_url == "https://x.com/a/status/902" and found.last_at == NOW - timedelta(days=0.5)


def test_two_platforms_one_with_engagements_only(env):
    store, metrics, _ = env
    baseline(store, metrics, "twitter", [100] * 5)
    # Bluesky has no impressions: engagements are likes + reposts + replies + quotes, as on the dashboard.
    for n in range(5):
        url = f"https://bsky.app/profile/a.bsky.social/post/base{n}"
        published(store, "bluesky", url, days_ago=3)
        sample(metrics, "bluesky", url, likes=6, reposts=2, replies=1, quotes=1)
    published(store, "twitter", "https://x.com/a/status/950", clip="atlas-loop", days_ago=2)
    sample(metrics, "twitter", "https://x.com/a/status/950", impressions=40)
    published(store, "bluesky", "https://bsky.app/profile/a.bsky.social/post/clip", clip="atlas-loop", days_ago=0.5)
    sample(metrics, "bluesky", "https://bsky.app/profile/a.bsky.social/post/clip", likes=1, reposts=1)

    found = perf(env)["atlas-loop"]
    bsky, x = found.platforms["bluesky"], found.platforms["twitter"]
    assert (bsky.measure, bsky.engagements, bsky.impressions, bsky.rating.label) == ("engagements", 2, None, WEAK)
    assert (bsky.rating.ratio, bsky.rating.baseline_median, bsky.rating.baseline_posts) == (0.2, 10, 6)
    assert (x.measure, x.rating.label, x.rating.ratio) == ("impressions", WEAK, 0.4)
    assert found.summary() in ("bsky weak, x weak", "x weak, bsky weak")
    assert found.last_url == "https://bsky.app/profile/a.bsky.social/post/clip"


def test_latest_sample_wins(env):
    store, metrics, _ = env
    baseline(store, metrics, "twitter", [100] * 5)
    url = "https://x.com/a/status/960"
    published(store, "twitter", url, clip="atlas-loop")
    sample(metrics, "twitter", url, hours_after=20, impressions=150)  # the latest
    sample(metrics, "twitter", url, hours_after=23, impressions=900)
    # The same moment written differently is still older than the latest.
    metrics.upsert(
        "post_metrics",
        [
            {
                "platform": "twitter",
                "external_id": url,
                "fetched_at": (NOW - timedelta(hours=21)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "impressions": 5,
            }
        ],
    )
    x = perf(env)["atlas-loop"].platforms["twitter"]
    assert (x.impressions, x.rating.label, x.rating.ratio) == (150, STRONG, 1.5)


def test_posts_older_than_the_window_are_not_in_the_baseline(env):
    store, metrics, _ = env
    baseline(store, metrics, "twitter", [100] * 4)
    baseline(store, metrics, "twitter", [10_000] * 9, days_ago=31)
    url = "https://x.com/a/status/970"
    published(store, "twitter", url, clip="atlas-loop", days_ago=40)  # an old clip still gets the current baseline
    sample(metrics, "twitter", url, impressions=100)

    x = perf(env)["atlas-loop"].platforms["twitter"]
    assert (x.rating.label, x.rating.baseline_posts, x.rating.why) == (NO_DATA, 4, "4 baseline posts, needs 5")
    assert "no data (4 baseline posts, needs 5)" in perf(env)["atlas-loop"].details()[0]

    baseline(store, metrics, "twitter", [100])
    x = perf(env)["atlas-loop"].platforms["twitter"]
    assert (x.rating.label, x.rating.baseline_posts, x.rating.baseline_median) == (TYPICAL, 5, 100)
    longer = PerformanceSettings(days=60)
    x = perf(env, longer)["atlas-loop"].platforms["twitter"]
    assert (x.rating.label, x.rating.baseline_posts) == (WEAK, 15)


def test_unposted_and_unsampled_clips_are_no_data(env):
    store, metrics, _ = env
    baseline(store, metrics, "twitter", [100] * 5)
    clips.add(store, "never", demo="atlas")
    clips.add(store, "pending-only", demo="atlas")
    draft_id, _ = store.add_draft(platform="twitter", title="", body="b", link="l", post_key="unsent")
    store.add_media(draft_id, kind="video", path="v", name="v", alt="a", size=1, removed="", clip_id="pending-only")
    published(store, "twitter", "https://x.com/a/status/980", clip="atlas-loop")  # out, but no sample yet
    published(store, "mastodon", "", clip="atlas-loop")  # out without a URL: draft:<id>, never sampled

    found = perf(env)
    assert found["never"].summary() == "-" and found["never"].last_url == "" and found["never"].last_at is None
    assert found["pending-only"].summary() == "-"  # a draft that never went out isn't a post
    loop = found["atlas-loop"]
    assert loop.summary() in ("x no data, mastodon no data", "mastodon no data, x no data")
    x = loop.platforms["twitter"]
    assert (x.posts, x.sampled, x.impressions, x.rating.why) == (1, 0, None, "no sample yet")
    assert loop.platforms["mastodon"].last_url == ""  # a draft:<id> placeholder is not a link


def test_missing_metrics_file_is_no_data_and_creates_nothing(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    clips.add(store, "c", demo="atlas")
    published(store, "twitter", "https://x.com/a/status/990", clip="c")
    path = tmp_path / "nowhere" / "metrics.sqlite"
    found = clip_performance(store, path, SETTINGS, now=NOW)
    assert found["c"].summary() == "x no data" and not path.parent.exists()
    assert performance.latest_samples(tmp_path / "s.sqlite") == {}  # a file without post_metrics
    store.close()


def test_config(tmp_path):
    def load(text):
        path = tmp_path / "c.yaml"
        path.write_text("pypi:\n  projects: [argus-atlas]\n" + text, encoding="utf-8")
        return load_config(path)

    assert load("").clips.performance == PerformanceSettings(1.5, 0.5, 5, 30)
    got = load("clips:\n  performance: {strong: 2, weak: 0.25, min_posts: 3, days: 14}\n").clips.performance
    assert got == PerformanceSettings(2.0, 0.25, 3, 14)
    for bad in (
        "{strong: 0.5, weak: 0.5}",
        "{strong: 0.4}",
        "{weak: 0}",
        "{weak: -1}",
        "{strong: yes}",
        "{strong: '2'}",
        "{min_posts: 0}",
        "{min_posts: 2.5}",
        "{days: -3}",
        "{days: true}",
        "{colour: red}",
        "[1.5]",
    ):
        with pytest.raises(ConfigError):
            load(f"clips:\n  performance: {bad}\n")


def test_cli_shows_ratings_and_the_numbers_behind_them(tmp_path, capsys):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("pypi:\n  projects: [argus-atlas]\n", encoding="utf-8")
    base = ["--config", str(cfg), "--env", str(tmp_path / "no.env"), "--store", str(tmp_path / "s.sqlite")]
    store = Store(tmp_path / "s.sqlite")
    clips.add(store, "a-posted", demo="atlas", status="posted")
    clips.add(store, "b-todo", demo="quarry")

    assert main([*base, "clips"]) == 0  # no metrics.sqlite yet: nothing created, nothing posted
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == [
        "clip",
        "demo",
        "format",
        "status",
        "fresh",
        "recorded",
        "version",
        "posts",
        "performance",
        "title",
    ]
    assert "Performance (" not in out and not (tmp_path / "metrics.sqlite").exists()

    metrics = Metrics(tmp_path / "metrics.sqlite")
    now = datetime.now(UTC)
    for n in range(5):
        url = f"https://x.com/a/status/{100 + n}"
        draft_id = published(store, "twitter", url)
        store.conn.execute("UPDATE drafts SET updated_at = ? WHERE id = ?", (now.isoformat(), draft_id))
        sample(metrics, "twitter", url, impressions=100)
    logged(store, "bluesky", "https://bsky.app/profile/a.bsky.social/post/x", clip="a-posted", days_ago=0)
    url = "https://x.com/a/status/200"
    draft_id = published(store, "twitter", url, clip="a-posted")
    store.conn.execute("UPDATE drafts SET updated_at = ? WHERE id = ?", (now.isoformat(), draft_id))
    sample(metrics, "twitter", url, impressions=400, engagements=12, link_clicks=3)
    metrics.close()
    store.close()

    assert main([*base, "clips"]) == 0
    out = capsys.readouterr().out
    rows = {line.split()[0]: line for line in out.splitlines()[1:3]}
    assert "bsky no data, x strong" in rows["a-posted"]
    assert rows["b-todo"].split()[7:9] == ["-", "-"]  # posts, performance
    assert (
        "Performance (each clip's median against the platform's own recent posts):\n"
        "  a-posted\n"
        "    bsky: no data (no sample yet); 1 post (0 sampled): - impressions, - engagements, - link clicks; "
        "median - engagements vs baseline median - over 0 posts\n"
        "    x: strong 4.00x; 1 post: 400 impressions, 12 engagements, 3 link clicks; "
        "median 400 impressions vs baseline median 100 over 6 posts\n"
    ) in out

    assert main([*base, "clips", "show", "a-posted"]) == 0
    out = capsys.readouterr().out
    assert "Performance (median against the platform's own recent posts):\n  bsky: no data" in out
    assert "\n  x: strong 4.00x;" in out
    assert main([*base, "clips", "show", "b-todo"]) == 0
    assert "Performance: no data (no post that used it has gone out)" in capsys.readouterr().out
