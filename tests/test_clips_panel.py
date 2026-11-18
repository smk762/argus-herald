"""The dashboard's Clips panel (clip_panel.py) and the CLI listing that shares its order."""

import json
import os
from datetime import UTC, datetime, timedelta

from starlette.testclient import TestClient

from argus_herald import clip_panel, clips
from argus_herald.config import PerformanceSettings, load_config
from argus_herald.metrics import Metrics
from argus_herald.performance import NO_DATA, STRONG, TYPICAL, WEAK, ClipPerformance, PlatformPerformance, Rating
from argus_herald.store import Store
from argus_herald.ui.app import create_app

NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)
COMMIT = "a" * 40


def recorded(store, clip_id, **fields):
    defaults = {
        "demo": "atlas",
        "project": "argus-atlas",
        "status": "recorded",
        "project_version": "0.4.0",
        "demo_repo": "smk762/argus-realms",
        "demo_commit": COMMIT,
        "title": clip_id.title(),
    }
    return clips.add(store, clip_id, **{**defaults, **fields})


def checked(store, clip, state, *, ui_files=(), commits=0, **fields):
    store.save_freshness(
        clip["id"],
        state=state,
        commits=commits,
        ui_files=json.dumps(list(ui_files)),
        files_changed=len(ui_files),
        branch="dev",
        head_commit="b" * 40,
        base_commit=clip["demo_commit"],
        base_version=clip["project_version"],
        **fields,
    )


def logged(store, platform, url, *, clip=None, at=None):
    at = at or datetime.now(UTC).isoformat()
    bid = store.log_broadcast(platform=platform, url=url, kind="demo", project="", subject="s", text="t", at=at)
    if clip:
        clips.link_broadcast(store, bid, clip)
    return bid


def rated(clip_id, *ratings, last_url="https://x.com/a/status/1", days_ago=2):
    """A clip's performance: one platform per (platform, rating, value)."""
    perf = ClipPerformance(clip_id)
    for i, (platform, label, value) in enumerate(ratings):
        perf.platforms[platform] = PlatformPerformance(
            platform=platform,
            measure="impressions" if platform == "twitter" else "engagements",
            posts=1,
            sampled=1,
            impressions=value if platform == "twitter" else None,
            engagements=value,
            rating=Rating(label, 1.0, value, value, 6),
            last_at=NOW - timedelta(days=days_ago + i),
            last_url=last_url,
        )
    return perf


def test_freshness_first_then_best_performance_and_the_cli_agrees(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    states = {
        "a-fresh": "fresh",
        "b-redo": "redo",
        "c-redo": "redo",
        "d-check": "check",
        "e-unknown": "unknown",
        "g-check": "check",
    }
    for clip_id, state in states.items():
        checked(store, recorded(store, clip_id), state, ui_files=["src/ui.ts"] if state == "redo" else (), commits=1)
    recorded(store, "f-todo", status="todo")
    performance = {
        "a-fresh": rated("a-fresh", ("twitter", STRONG, 900)),
        "b-redo": rated("b-redo", ("twitter", WEAK, 10)),
        # Its best platform decides: strong on Bluesky beats weak on X.
        "c-redo": rated("c-redo", ("twitter", WEAK, 10), ("bluesky", STRONG, 40)),
        "e-unknown": rated("e-unknown", ("twitter", TYPICAL, 100)),
        "g-check": rated("g-check", ("bluesky", STRONG, 50)),
    }
    panel = clip_panel.build(store.clips(), store.clip_freshness(), performance, NOW)
    order = [r.id for r in panel.rows]
    assert order == ["c-redo", "b-redo", "g-check", "d-check", "e-unknown", "a-fresh", "f-todo"]
    assert [r.id for r in panel.rows if r.rerecord_first] == ["c-redo"]
    by_id = {r.id: r for r in panel.rows}
    assert (by_id["c-redo"].best, by_id["d-check"].best, by_id["f-todo"].freshness) == (STRONG, NO_DATA, "not checked")
    assert {r.freshness for r in panel.rows} <= set(clip_panel.FRESHNESS)

    listing = clips.report(store, now=NOW, performance=performance)
    assert [line.split()[0] for line in listing.splitlines()[1:8]] == order
    assert "  c-redo (redo, strong: re-record first): 1 commit on dev" in listing
    assert "  b-redo (redo): " in listing
    store.close()


def test_platform_badges_numbers_and_the_last_post(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    recorded(store, "x1", status="posted")
    recorded(store, "x2", status="posted")
    recorded(store, "x3", status="posted")
    performance = {
        "x1": rated("x1", ("twitter", STRONG, 1800), ("bluesky", NO_DATA, 7)),
        "x2": rated("x2", ("bluesky", TYPICAL, 9), last_url="javascript:alert(1)"),
        "x3": rated("x3", ("twitter", WEAK, 3), last_url="http://x.com/a/status/2"),
    }
    rows = {r.id: r for r in clip_panel.build(store.clips(), {}, performance, NOW).rows}
    second, first = rows["x1"].platforms  # the CLI's order: most posts, then name (bsky before x)
    assert (first.platform, first.rating, first.value, first.measure) == ("X", STRONG, 1800, "impressions")
    assert (second.platform, second.rating, second.measure) == ("Bluesky", NO_DATA, "engagements")
    assert "1,800 impressions" in first.detail
    # The latest post across platforms: X here (two days ago), linked.
    assert rows["x1"].last_posted == (NOW - timedelta(days=2)).date()
    assert rows["x1"].last_url == "https://x.com/a/status/1"
    # Only https:// links; the date still shows.
    assert rows["x2"].last_url == rows["x3"].last_url == ""
    assert rows["x2"].last_posted is not None
    store.close()


def test_coverage_status_counts_and_an_empty_registry(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    assert clip_panel.load(store, tmp_path / "metrics.sqlite", PerformanceSettings(), NOW) == clip_panel.ClipsPanel(
        (), (), (), ()
    )
    recorded(store, "a1", demo="atlas")
    recorded(store, "a2", demo="atlas", status="todo")
    recorded(store, "q1", demo="quarry", status="stale")
    recorded(store, "r1", demo="realms", status="posted")
    recorded(store, "w1", demo="", status="hold")
    panel = clip_panel.load(store, tmp_path / "metrics.sqlite", PerformanceSettings(), NOW)
    assert panel.covered == ("atlas", "realms") and panel.missing == ("quarry",)
    assert panel.statuses == (("todo", 1), ("recorded", 1), ("posted", 1), ("stale", 1), ("hold", 1))
    store.close()


def test_what_changed_and_the_age_of_a_failed_check(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    files = [f"src/ui/{n}.ts" for n in "abcde"]
    clip = recorded(store, "redo-me", stale_if="the legend moves")
    checked(store, clip, "redo", ui_files=files, commits=4)
    failing = recorded(store, "failing")
    checked(store, failing, "check", commits=1)
    store.freshness_failed(
        "failing", "GET api.github.com failed with HTTP 500", base_commit=COMMIT, base_version="0.4.0"
    )
    recorded(store, "never")
    store.freshness_failed("never", "GET api.github.com failed with HTTP 500", base_commit=COMMIT, base_version="0.4.0")
    later = datetime.now(UTC) + timedelta(days=3)
    rows = {r.id: r for r in clip_panel.build(store.clips(), store.clip_freshness(), {}, later).rows}

    why = rows["redo-me"].why
    assert why[0] == (
        "4 commits on dev since aaaaaaa; 5 UI files changed: src/ui/a.ts, src/ui/b.ts, src/ui/c.ts +2 more."
    )
    assert "Stale if: the legend moves" in why and rows["redo-me"].age == ""
    assert (rows["failing"].freshness, rows["failing"].age) == ("check", "as of 3d")
    assert any(line.startswith("The last check failed 3d ago") for line in rows["failing"].why)
    # A check that never completed has no earlier result to date.
    assert (rows["never"].freshness, rows["never"].age) == ("unknown", "")
    line = next(line for line in clips.report(store, now=later).splitlines() if line.startswith("failing "))
    assert "check (as of 3d)" in line
    store.close()


def test_missing_metrics_file_is_no_data_and_creates_nothing(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    clips.add(store, "a1", demo="atlas", status="posted")
    logged(store, "bluesky", "https://bsky.app/profile/a/post/1", clip="a1")
    (row,) = clip_panel.load(store, tmp_path / "metrics.sqlite", PerformanceSettings()).rows
    assert not (tmp_path / "metrics.sqlite").exists()
    (badge,) = row.platforms
    assert (badge.platform, badge.rating, badge.value) == ("Bluesky", NO_DATA, None)
    assert row.last_url == "https://bsky.app/profile/a/post/1" and row.last_posted is not None
    store.close()


# ---- the page ----------------------------------------------------------------------------------


def _app(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("pypi: {projects: [argus-atlas]}\ndestinations:\n  twitter: {enabled: true, username: a}\n")
    config = load_config(path)
    config.store_path = tmp_path / "state.sqlite"
    env = {
        "ARGUS_HERALD_UI_SECRET": "s",
        "ARGUS_HERALD_OPSEC_DENYLIST": os.environ["ARGUS_HERALD_OPSEC_DENYLIST"],
        "AI_AUDIT_OPSEC_DENYLIST": os.environ["AI_AUDIT_OPSEC_DENYLIST"],
    }
    return create_app(config, env), config


def _sample(metrics, url, impressions):
    metrics.upsert(
        "post_metrics",
        [
            {
                "platform": "twitter",
                "external_id": url,
                "fetched_at": datetime.now(UTC).isoformat(),
                "likes": 1,
                "reposts": 0,
                "replies": 0,
                "quotes": 0,
                "impressions": impressions,
                "engagements": None,
                "link_clicks": None,
                "profile_visits": None,
                "source": "api",
            }
        ],
    )


def test_the_dashboard_shows_the_panel_with_the_strong_redo_marker(tmp_path):
    app, config = _app(tmp_path)
    client = TestClient(app)
    assert "No clips yet." in client.get("/dashboard").text

    store = Store(config.store_path)
    clip = recorded(store, "atlas-loop", status="posted", title="Atlas loop")
    checked(store, clip, "redo", ui_files=["src/legend.ts"], commits=2)
    recorded(store, "quarry-loop", demo="quarry", status="todo")
    metrics = Metrics(tmp_path / "metrics.sqlite")
    for n in range(5):  # the account's own recent posts: the baseline
        url = f"https://x.com/a/status/{n}"
        logged(store, "twitter", url)
        _sample(metrics, url, 100)
    url = "https://x.com/a/status/99"
    logged(store, "twitter", url, clip="atlas-loop")
    _sample(metrics, url, 900)
    metrics.close()
    store.close()

    page = client.get("/dashboard").text
    assert '<h2 id="clips-h">Clips</h2>' in page
    assert "re-record first" in page and 'class="rerecord"' in page
    assert ">redo</span>" in page and ">strong</span>" in page and "900 impressions" in page
    assert "<summary>What changed</summary>" in page and "src/legend.ts" in page
    assert f'<a href="{url}" target="_blank" rel="noopener noreferrer">' in page
    assert "Demos with a current clip: 1/2. None yet: quarry" in page
    assert page.index('data-clip="atlas-loop"') < page.index('data-clip="quarry-loop"')
    # The CSV exports don't change.
    assert client.get("/dashboard/broadcasts.csv").status_code == 200


def test_user_text_and_links_render_inert(tmp_path):
    app, config = _app(tmp_path)
    store = Store(config.store_path)
    clip = recorded(
        store,
        "evil",
        title="<script>alert(1)</script>",
        demo='"><img src=x onerror=alert(2)>',
        stale_if="<b>bold</b>",
        status="posted",
    )
    checked(store, clip, "redo", ui_files=["<svg onload=alert(3)>.ts"], commits=1)
    for n, url in enumerate(("javascript:alert(4)", "http://x.com/a/status/5")):
        logged(store, "twitter", url, clip="evil", at=f"2026-11-1{n}T12:00:00+00:00")
    store.close()

    page = TestClient(app).get("/dashboard").text
    start = page.index('id="clips"')
    panel = page[start:]
    assert "<script>alert(1)" not in panel and "&lt;script&gt;alert(1)&lt;/script&gt;" in panel
    assert "<img" not in panel and "<svg onload" not in panel and "<b>bold" not in panel
    assert "&lt;svg onload=alert(3)&gt;.ts" in panel and "&lt;b&gt;bold&lt;/b&gt;" in panel
    assert "javascript:" not in panel and 'href="http://' not in panel
    assert "2026-11-11" in panel  # the latest post's date, unlinked
