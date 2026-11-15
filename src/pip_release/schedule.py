"""Scheduled publishing: approved drafts go out at their destination's time slots.

On approve, a draft gets its destination's next free slot (UTC), at least `min_gap_hours` from
that destination's other posts. Drafts of one post (the same post key) go out together: a draft
whose sibling is already scheduled takes the same time, shifted by its own `offset_minutes`.

The watcher publishes drafts when they're due. API platforms post; manual platforms (X,
Patreon) are handed off with a notification, ready to post. A slot missed by more than
`grace_hours` (the watcher was down) isn't posted late: the draft stays approved, loses its
time, and gets a note.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pip_release.config import Schedule
from pip_release.models import Draft
from pip_release.store import Store

LOOKAHEAD_WEEKS = 8
LEAD = timedelta(minutes=5)  # never schedule closer than this to now


def parse(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()


def slot_times(schedule: Schedule, after: datetime, weeks: int = LOOKAHEAD_WEEKS):
    """Slot times strictly after `after`, in order, for the next few weeks."""
    after = after.astimezone(timezone.utc)
    monday = (after - timedelta(days=after.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    for week in range(weeks + 1):
        for day, hour, minute in schedule.slots:
            when = monday + timedelta(weeks=week, days=day, hours=hour, minutes=minute)
            if when > after:
                yield when


def _clear(when: datetime, taken: list[datetime], gap: timedelta) -> bool:
    return all(abs(when - other) >= gap for other in taken)


def free_slots(store: Store, platform: str, schedule: Schedule, now: datetime, count: int = 6,
               ignore: int | None = None) -> list[datetime]:
    """The next free times for a platform, with its offset applied."""
    taken = [parse(d.publish_at) for d in store.scheduled(platform) if d.id != ignore and d.publish_at]
    # Posts already out count too, so a new slot keeps its distance from them.
    taken += [parse(t) for t in store.post_times(platform) if t]
    gap = timedelta(hours=schedule.min_gap_hours)
    offset = timedelta(minutes=schedule.offset_minutes)
    out = []
    for base in slot_times(schedule, now + LEAD - offset):
        when = base + offset
        if _clear(when, taken, gap):
            out.append(when)
            if len(out) == count:
                break
    return out


def choose(store: Store, draft: Draft, schedule: Schedule, now: datetime,
           schedules: dict[str, Schedule] | None = None) -> datetime | None:
    """The time this draft should go out: with its post's other drafts, else the next free slot."""
    schedules = schedules or {}
    offset = timedelta(minutes=schedule.offset_minutes)
    for sibling in store.list_drafts(post_key=draft.post_key):
        if sibling.id == draft.id or not sibling.publish_at or sibling.status != "approved":
            continue
        theirs = schedules.get(sibling.platform)
        base = parse(sibling.publish_at) - timedelta(minutes=theirs.offset_minutes if theirs else 0)
        when = base + offset
        taken = [parse(d.publish_at) for d in store.scheduled(draft.platform) if d.id != draft.id and d.publish_at]
        if when > now + LEAD and _clear(when, taken, timedelta(hours=schedule.min_gap_hours)):
            return when
    slots = free_slots(store, draft.platform, schedule, now, count=1, ignore=None)
    return slots[0] if slots else None


@dataclass
class DueResult:
    kind: str  # published | handed_off | missed | failed
    draft: Draft
    detail: str


def publish_due(store: Store, publishers: dict, schedules: dict[str, Schedule], env, http, gate,
                now: datetime) -> list[DueResult]:
    """Publish or hand off every approved draft whose time has come."""
    from pip_release.workflow import ReviewError, publish_draft

    results: list[DueResult] = []
    for draft in store.due(iso(now + timedelta(seconds=59))):
        schedule = schedules.get(draft.platform)
        grace = timedelta(hours=schedule.grace_hours if schedule else 6)
        when = parse(draft.publish_at)
        if now - when > grace:
            store.set_publish_at(draft.id, None)
            note = (f"Missed its slot at {when:%a %d %b %H:%M} UTC (the watcher wasn't running). "
                    "Pick a new time or publish now.")
            store.set_note(draft.id, note)
            results.append(DueResult("missed", draft, note))
            continue
        publisher = publishers.get(draft.platform)
        if publisher is None:
            results.append(DueResult("failed", draft, f"no publisher for {draft.platform}"))
            continue
        if gate is None:
            results.append(DueResult("failed", draft, "privacy gate unavailable; nothing is published without it"))
            continue
        try:
            result = publish_draft(store, publisher, draft, env, http, gate=gate)
        except (ReviewError, OSError, ValueError) as exc:
            results.append(DueResult("failed", draft, str(exc)))
            continue
        results.append(DueResult("handed_off" if result.handed_off else "published", draft, result.detail))
    return results


def next_due_in(store: Store, now: datetime) -> float | None:
    """Seconds until the next scheduled draft, for the watcher's sleep."""
    upcoming = [parse(d.publish_at) for d in store.scheduled() if d.publish_at]
    if not upcoming:
        return None
    return max(0.0, (min(upcoming) - now).total_seconds())


# ---- slot suggestions --------------------------------------------------------------------

MIN_POSTS = 6


def suggest(metrics_db: Path) -> str:
    """Engagement by weekday and hour per platform, from post metrics (#12), and what to try."""
    if not metrics_db.exists():
        return "No metrics yet: run pip-release collect."
    conn = sqlite3.connect(f"file:{metrics_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    has = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "post_metrics" not in has:
        return ("No post engagement collected yet. Bluesky and Mastodon posts and the X analytics import "
                "fill it in; suggestions need a few weeks of posts.")
    rows = conn.execute(
        """
        SELECT e.source AS platform, e.at AS at,
               MAX(COALESCE(p.likes, 0) + COALESCE(p.reposts, 0) * 2 + COALESCE(p.replies, 0) * 2
                   + COALESCE(p.quotes, 0) * 2) AS score
        FROM events e JOIN post_metrics p ON p.external_id = e.url AND p.platform = e.source
        WHERE e.kind = 'broadcast'
        GROUP BY e.source, e.url
        """
    ).fetchall()
    if not rows:
        return "No published posts have engagement data yet."
    by_platform: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for row in rows:
        by_platform[row["platform"]].append((parse(row["at"]), float(row["score"] or 0)))
    lines = []
    for platform, posts in sorted(by_platform.items()):
        lines.append(f"{platform}: {len(posts)} posts")
        buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
        for when, score in posts:
            buckets[(when.weekday(), when.hour // 4 * 4)].append(score)
        ranked = sorted(buckets.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))
        for (day, hour), scores in ranked:
            avg = sum(scores) / len(scores)
            lines.append(f"  {('Mon','Tue','Wed','Thu','Fri','Sat','Sun')[day]} {hour:02d}-{hour + 4:02d} UTC  "
                         f"avg engagement {avg:.1f}  ({len(scores)} post{'s' if len(scores) != 1 else ''})")
        if len(posts) < MIN_POSTS:
            lines.append(f"  Too few posts to suggest a change (need {MIN_POSTS}).")
        else:
            (day, hour), scores = ranked[0]
            lines.append(f"  Try a slot around {('Mon','Tue','Wed','Thu','Fri','Sat','Sun')[day]} {hour:02d}:00 UTC "
                         f"(based on {len(scores)} posts; config is not changed).")
    return "\n".join(lines)
