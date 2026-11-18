"""Clip performance: how the posts that used each clip did, per platform, against the account's own posts.

The join, computed on read (nothing is stored): a clip -> the posts that used it and went out
(published drafts it is attached to, including handed-off drafts once confirmed, and hand-logged
broadcasts that name it) -> each post's latest sample in metrics.sqlite's post_metrics.

Per clip and platform: posts, and the sums of impressions, engagements and link clicks over
those posts' latest samples. A sample's engagements are its `engagements` value where the
platform reports one (the X analytics import), else likes + reposts + replies + quotes, as on the
dashboard.

The rating compares medians, so platforms with different audience sizes read the same way:

    ratio = median over the clip's posts of the measure
            / median of the measure over that platform's posts from the last `days` days

The measure is impressions on a platform where any post has an impression count, else
engagements. The baseline window ends now and covers every post that went out on that platform
in it and has a sample, the clip's own posts included, so it is one number per platform that
every clip is held against.

A post younger than `min_age_hours` (24) is left out of both medians: its first samples are
small and would pull the baseline down and make a new clip look weak. Age is measured from the
post's time (the draft's last update, or the logged broadcast's time) to now; a post exactly
`min_age_hours` old is old enough. A young post still counts in the clip's posts and sums and
still sets its last post. A post whose time can't be read is never too young: it stays out of
the baseline, as it is outside every window, and stays in the clip's own median. 0 turns the rule off.

    strong   ratio >= strong (1.5)
    weak     ratio <= weak (0.5)
    typical  in between
    no data  the clip has no sampled post there (or only ones younger than min_age_hours), fewer
             than min_posts (5) baseline posts, or a baseline median of 0
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

from argus_herald.config import PerformanceSettings
from argus_herald.dashboard import connect, engagement_score, platform_key
from argus_herald.metrics import PostRef, posts_out
from argus_herald.store import Store

STRONG, TYPICAL, WEAK, NO_DATA = "strong", "typical", "weak", "no data"
# Short names for the listing; a platform the dashboard doesn't know keeps its own name.
_SHORT = {"twitter": "x", "bluesky": "bsky"}


@dataclass(frozen=True)
class Rating:
    label: str
    ratio: float | None = None
    clip_median: float | None = None
    baseline_median: float | None = None
    baseline_posts: int = 0
    why: str = ""  # for no data: the reason


def rate(
    clip_values: Sequence[float],
    baseline_values: Sequence[float],
    settings: PerformanceSettings,
    empty: str = "no sample yet",
) -> Rating:
    """Rate a clip's per-post measures against a platform's baseline measures. Pure.

    `empty` is the no data reason when the clip has no values."""
    count = len(baseline_values)
    base = median(baseline_values) if baseline_values else None
    if not clip_values:
        return Rating(NO_DATA, baseline_median=base, baseline_posts=count, why=empty)
    mid = median(clip_values)
    if count < settings.min_posts:
        why = f"{count} baseline post{'s' * (count != 1)}, needs {settings.min_posts}"
        return Rating(NO_DATA, clip_median=mid, baseline_median=base, baseline_posts=count, why=why)
    if not base:
        return Rating(NO_DATA, clip_median=mid, baseline_median=base, baseline_posts=count, why="baseline median is 0")
    ratio = mid / base
    label = STRONG if ratio >= settings.strong else WEAK if ratio <= settings.weak else TYPICAL
    return Rating(label, ratio, mid, base, count)


@dataclass
class PlatformPerformance:
    platform: str  # the dashboard's platform key (twitter, bluesky, ...), or the platform's own name
    measure: str  # "impressions" or "engagements": what the rating compares
    posts: int = 0  # posts that went out
    sampled: int = 0  # of those, posts with a sample
    impressions: int | None = None
    engagements: int | None = None
    link_clicks: int | None = None
    rating: Rating = field(default_factory=lambda: Rating(NO_DATA, why="no sample yet"))
    last_at: datetime | None = None
    last_url: str = ""  # https:// only; empty when the post has no link

    @property
    def short(self) -> str:
        return short_name(self.platform)


@dataclass
class ClipPerformance:
    clip_id: str
    platforms: dict[str, PlatformPerformance] = field(default_factory=dict)

    @property
    def last(self) -> PlatformPerformance | None:
        """The platform entry holding the clip's most recent post (its date and URL)."""
        dated = [p for p in self.platforms.values() if p.last_at is not None]
        return max(dated, key=lambda p: p.last_at) if dated else None

    @property
    def last_at(self) -> datetime | None:
        last = self.last
        return last.last_at if last else None

    @property
    def last_url(self) -> str:
        last = self.last
        return last.last_url if last else ""

    def summary(self) -> str:
        """The listing's cell: `x strong, bsky typical`, or `-` for a clip never posted."""
        if not self.platforms:
            return "-"
        return ", ".join(f"{p.short} {p.rating.label}" for p in self._ordered())

    def details(self) -> list[str]:
        """One line per platform: the numbers behind the rating."""
        return [_detail(p) for p in self._ordered()]

    def _ordered(self) -> list[PlatformPerformance]:
        return sorted(self.platforms.values(), key=lambda p: (-p.posts, p.short))


def short_name(platform: str) -> str:
    return _SHORT.get(platform, platform)


def platform_group(name: str) -> str:
    """Posts are grouped the way the dashboard groups them, so `x` and `twitter` are one platform."""
    key = platform_key(name)
    return (name or "other").strip().lower() if key == "other" else key


def clip_performance(
    store: Store, metrics_file: Path, settings: PerformanceSettings, now: datetime | None = None
) -> dict[str, ClipPerformance]:
    """Every clip's performance, by clip id, in a handful of queries. A clip never posted has no platforms.

    The dashboard reads this too: per clip, per platform, the rating and the numbers behind it,
    and the clip's last post (`last_at`, `last_url`)."""
    now = now or datetime.now(UTC)
    links: dict[tuple[str, int], set[str]] = defaultdict(set)
    for clip_id, via, ref in store.clip_links():
        links[(via, ref)].add(clip_id)
    posts = [ref for ref, _row in posts_out(store)]
    samples = latest_samples(metrics_file)
    return compute([c["id"] for c in store.clips()], posts, links, samples, settings, now)


def compute(
    clip_ids: Iterable[str],
    posts: Sequence[PostRef],
    links: dict[tuple[str, int], set[str]],
    samples: dict[tuple[str, str], dict],
    settings: PerformanceSettings,
    now: datetime,
) -> dict[str, ClipPerformance]:
    """The join over plain rows: posts that went out, which clips each used, and the latest sample per post."""
    by_group: dict[str, list[tuple[PostRef, dict | None, datetime | None]]] = defaultdict(list)
    for ref in posts:
        by_group[platform_group(ref.platform)].append((ref, samples.get((ref.platform, ref.url)), _when(ref.at)))
    measures = {
        group: "impressions" if any(s and s["impressions"] is not None for _r, s, _at in items) else "engagements"
        for group, items in by_group.items()
    }
    since = now - timedelta(days=settings.days)
    settled = now - timedelta(hours=settings.min_age_hours)  # posted at or before this: old enough to rate
    baselines: dict[str, list[float]] = {}
    for group, items in by_group.items():
        values = [
            _measure(s, measures[group]) for _r, s, at in items if s and at is not None and since <= at <= settled
        ]
        baselines[group] = [v for v in values if v is not None]

    out = {clip_id: ClipPerformance(clip_id) for clip_id in clip_ids}
    used: dict[tuple[str, str], list[tuple[PostRef, dict | None, datetime | None]]] = defaultdict(list)
    for group, items in by_group.items():
        for item in items:
            for clip_id in links.get((item[0].via, item[0].id), ()):
                if clip_id in out:
                    used[(clip_id, group)].append(item)
    for (clip_id, group), items in sorted(used.items()):
        measure = measures[group]
        sampled = [s for _r, s, _at in items if s]
        rated = [s for _r, s, at in items if s and (at is None or at <= settled)]
        values = [v for v in (_measure(s, measure) for s in rated) if v is not None]
        empty = f"posted less than {settings.min_age_hours}h ago" if len(rated) < len(sampled) else "no sample yet"
        dated = [(at, ref) for ref, _s, at in items if at is not None]
        last_at, last_ref = max(dated, key=lambda pair: pair[0]) if dated else (None, None)
        out[clip_id].platforms[group] = PlatformPerformance(
            platform=group,
            measure=measure,
            posts=len(items),
            sampled=len(sampled),
            impressions=_sum(s["impressions"] for s in sampled),
            engagements=_sum(_engagements(s) for s in sampled),
            link_clicks=_sum(s["link_clicks"] for s in sampled),
            rating=rate(values, baselines[group], settings, empty),
            last_at=last_at,
            last_url=last_ref.url if last_ref and last_ref.url.startswith("https://") else "",
        )
    return out


_COLUMNS = ("platform", "external_id", "fetched_at", "impressions", "engagements", "likes", "reposts", "replies")
_COLUMNS += ("quotes", "link_clicks")


def latest_samples(metrics_file: Path) -> dict[tuple[str, str], dict]:
    """The latest post_metrics sample per (platform, external_id). Empty, and nothing created,
    when metrics.sqlite doesn't exist yet."""
    try:
        conn = connect(metrics_file)
    except sqlite3.Error:
        return {}
    if conn is None:
        return {}
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'post_metrics'").fetchone():
            return {}
        rows = conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM post_metrics").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    latest: dict[tuple[str, str], tuple[datetime, dict]] = {}
    for row in rows:
        # Parsed times, not strings: stored stamps differ in separator, offset and precision.
        at = _when(row["fetched_at"])
        if at is None:
            continue
        key = (row["platform"], row["external_id"])
        if key not in latest or at >= latest[key][0]:
            latest[key] = (at, dict(row))
    return {key: row for key, (_at, row) in latest.items()}


def _when(value: object) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _engagements(sample: dict) -> int:
    return int(sample["engagements"]) if sample["engagements"] is not None else engagement_score(sample)


def _measure(sample: dict, measure: str) -> float | None:
    if measure == "impressions":
        return None if sample["impressions"] is None else float(sample["impressions"])
    return float(_engagements(sample))


def _sum(values: Iterable[int | None]) -> int | None:
    known = [int(v) for v in values if v is not None]
    return sum(known) if known else None


def _number(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.0f}" if value == int(value) or abs(value) >= 100 else f"{value:,.1f}"


def _detail(p: PlatformPerformance) -> str:
    r = p.rating
    head = f"{p.short}: {r.label}" + (f" {r.ratio:.2f}x" if r.ratio is not None else f" ({r.why})" if r.why else "")
    posts = f"{p.posts} post{'s' * (p.posts != 1)}" + (f" ({p.sampled} sampled)" if p.sampled != p.posts else "")
    numbers = (
        f"{_number(p.impressions)} impressions, {_number(p.engagements)} engagements, "
        f"{_number(p.link_clicks)} link clicks"
    )
    clip = f"median {_number(r.clip_median)} {p.measure}"
    base = f"baseline median {_number(r.baseline_median)} over {r.baseline_posts} post{'s' * (r.baseline_posts != 1)}"
    return f"{head}; {posts}: {numbers}; {clip} vs {base}"
