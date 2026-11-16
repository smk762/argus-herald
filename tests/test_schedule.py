import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from argus_herald.config import ConfigError, Destination, Schedule, load_config
from argus_herald.models import Post
from argus_herald.platforms import DiscordPublisher, PatreonPublisher, TwitterPublisher
from argus_herald.schedule import free_slots, iso, next_due_in, parse, publish_due, slot_times, suggest
from argus_herald.store import Store
from argus_herald.workflow import approve, compose, edit_draft

UTC = UTC
NOW = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)  # a Monday
TUE_FRI = Schedule(slots=((1, 8, 0), (4, 8, 0)), min_gap_hours=24)


def x(schedule=TUE_FRI):
    return TwitterPublisher().configure(Destination(schedule=schedule))


def test_config(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        "pypi: {projects: [a]}\ndestinations:\n  twitter:\n    schedule:\n"
        "      slots: [Tue 08:00, friday 8:00]\n      offset_minutes: 15\n",
        encoding="utf-8",
    )
    sched = load_config(path).destinations["twitter"].schedule
    assert sched.slots == ((1, 8, 0), (4, 8, 0)) and sched.offset_minutes == 15 and sched.grace_hours == 6
    for bad in (
        "{slots: []}",
        "{slots: [Tue 25:00]}",
        "{slots: [Someday 08:00]}",
        "{slots: [Tue 08:00], grace_hours: -1}",
    ):
        path.write_text(f"pypi: {{projects: [a]}}\ndestinations:\n  twitter:\n    schedule: {bad}\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(path)


def test_slots_respect_gap_and_offset(tmp_path):
    assert [w.isoformat() for w in list(slot_times(TUE_FRI, NOW))[:3]] == [
        "2026-11-24T08:00:00+00:00",
        "2026-11-27T08:00:00+00:00",
        "2026-12-01T08:00:00+00:00",
    ]
    store = Store(tmp_path / "s.sqlite")
    draft_id, _ = store.add_draft(project="p", version="1", platform="twitter", title="", body="b", link="l")
    store.transition(draft_id, ("pending",), "approved")
    store.set_publish_at(draft_id, "2026-11-24T08:00:00+00:00")
    assert free_slots(store, "twitter", TUE_FRI, NOW, count=2)[0].isoformat() == "2026-11-27T08:00:00+00:00"
    assert free_slots(store, "twitter", TUE_FRI, NOW, ignore=draft_id)[0].isoformat() == "2026-11-24T08:00:00+00:00"
    shifted = Schedule(slots=TUE_FRI.slots, offset_minutes=30)
    assert free_slots(store, "patreon", shifted, NOW)[0].isoformat() == "2026-11-24T08:30:00+00:00"
    store.close()


def test_approve_schedules_and_siblings_line_up(tmp_path, gate):
    store = Store(tmp_path / "s.sqlite")
    ids = compose(store, [TwitterPublisher(), PatreonPublisher()], Post("demo", "Atlas", "Coastlines", "https://e.org"))
    tw = approve(store, x(), ids[0], None, gate=gate)
    assert tw.publish_at is not None and parse(tw.publish_at).weekday() in (1, 4)
    patreon_sched = Schedule(slots=((2, 12, 0),), offset_minutes=20, min_gap_hours=1)
    pa = PatreonPublisher().configure(Destination(schedule=patreon_sched))
    pa_approved = approve(store, pa, ids[1], None, gate=gate, schedules={"twitter": TUE_FRI})
    assert parse(pa_approved.publish_at) == parse(tw.publish_at) + timedelta(minutes=20)  # together, own offset
    edit_draft(store, x(), ids[0], "", "Coastlines, painted")
    assert store.get(ids[0]).publish_at is None and store.get(ids[0]).status == "pending"
    assert approve(store, x(), ids[0], None, gate=gate, schedule=False).publish_at is None
    store.close()


def test_publish_due_hands_off_posts_and_skips_missed(tmp_path, gate):
    store = Store(tmp_path / "s.sqlite")
    made = {}
    for platform in ("twitter", "patreon", "discord"):
        made[platform], _ = store.add_draft(
            project="p", version="1", platform=platform, title="t", body="hello", link="l"
        )
        store.transition(made[platform], ("pending",), "approved")
    store.set_publish_at(made["twitter"], iso(NOW - timedelta(minutes=10)))
    store.set_publish_at(made["patreon"], iso(NOW - timedelta(hours=7)))  # beyond the 6 h grace
    store.set_publish_at(made["discord"], iso(NOW + timedelta(hours=2)))
    pubs = {"twitter": TwitterPublisher(), "patreon": PatreonPublisher(), "discord": DiscordPublisher()}
    results = publish_due(store, pubs, {}, {}, None, gate, NOW)
    assert {(r.draft.platform, r.kind) for r in results} == {("twitter", "handed_off"), ("patreon", "missed")}
    assert store.get(made["twitter"]).status == "handed_off"
    missed = store.get(made["patreon"])
    assert missed.status == "approved" and missed.publish_at is None and missed.note.startswith("Missed its slot")
    assert store.get(made["discord"]).status == "approved"
    assert next_due_in(store, NOW) == pytest.approx(7200)
    later = publish_due(store, pubs, {}, {}, None, None, NOW + timedelta(hours=3))
    assert [(r.kind, r.detail.split(";")[0]) for r in later] == [("failed", "privacy gate unavailable")]
    store.close()


def test_cli_schedule_commands(tmp_path, capsys):
    from argus_herald.cli import main

    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "pypi: {projects: [demo]}\ndestinations:\n  twitter:\n    schedule: {slots: [Tue 08:00, Fri 08:00]}\n",
        encoding="utf-8",
    )
    base = ["--config", str(cfg), "--env", str(tmp_path / "no.env"), "--store", str(tmp_path / "s.sqlite")]
    store = Store(tmp_path / "s.sqlite")
    draft_id, _ = store.add_draft(project="demo", version="1", platform="twitter", title="", body="hello", link="l")
    store.close()
    assert main([*base, "schedule", str(draft_id)]) == 2  # not approved yet
    assert main([*base, "approve", str(draft_id)]) == 0
    assert "scheduled for 20" in capsys.readouterr().out
    assert main([*base, "schedule", str(draft_id), "--at", "2099-01-02T03:04Z"]) == 0
    assert main([*base, "scheduled"]) == 0 and "2099-01-02T03:04:00+00:00  #1" in capsys.readouterr().out
    assert main([*base, "schedule", str(draft_id), "--at", "2001-01-01T00:00Z"]) == 2
    assert main([*base, "schedule", str(draft_id), "--clear"]) == 0
    assert main([*base, "scheduled"]) == 0 and "Nothing scheduled." in capsys.readouterr().out


def test_suggest(tmp_path):
    db = tmp_path / "metrics.sqlite"
    assert "run argus-herald collect" in suggest(db)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE events (kind, source, project, version, at, url, detail)")
    assert "No post engagement" in suggest(db)
    conn.execute(
        "CREATE TABLE post_metrics (platform, external_id, fetched_at, likes, reposts, replies, quotes, impressions)"
    )
    # Four Friday-morning posts that did well, three Tuesday-evening posts that didn't.
    posts = [(datetime(2026, 12, 4, 8, tzinfo=UTC) + timedelta(weeks=w), 10) for w in range(4)]
    posts += [(datetime(2026, 12, 1, 18, tzinfo=UTC) + timedelta(weeks=w), 1) for w in range(3)]
    for i, (when, likes) in enumerate(posts):
        url = f"https://bsky.app/p/{i}"
        conn.execute("INSERT INTO events VALUES ('broadcast', 'bluesky', '', '', ?, ?, '')", (when.isoformat(), url))
        conn.execute("INSERT INTO post_metrics VALUES ('bluesky', ?, 't', ?, 0, 0, 0, 0)", (url, likes))
    conn.commit()
    report = suggest(db)
    assert report.startswith("bluesky: 7 posts") and "Try a slot around Fri 08:00 UTC (based on 4 posts" in report
