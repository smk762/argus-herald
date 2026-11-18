"""The dashboard's Clips panel: every clip's status, freshness and performance at a glance.

`build` turns the registry, the stored freshness checks and `performance.clip_performance` into
plain rows for the template, in the same order as `argus-herald clips` (`clips.ordered`):
freshness first, then the clip's best rating. Nothing here is rendered; the template escapes
every piece of text, and a post is linked only when its URL is https://.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from argus_herald import clips as registry
from argus_herald import freshness
from argus_herald.config import PerformanceSettings
from argus_herald.dashboard import platform_label
from argus_herald.models import CLIP_STATUSES
from argus_herald.performance import ClipPerformance, clip_performance
from argus_herald.store import Store

NOT_CHECKED = "not checked"
FRESHNESS = (*freshness.ORDER, NOT_CHECKED)  # every freshness badge's text


@dataclass(frozen=True)
class PlatformBadge:
    platform: str  # display name: X, Bluesky, ...
    rating: str  # strong / typical / weak / no data
    measure: str  # impressions or engagements: what the rating compares
    value: int | None  # that measure's total over the clip's sampled posts there
    detail: str  # the numbers behind the rating, one line


@dataclass(frozen=True)
class ClipRow:
    id: str
    title: str
    demo: str
    format: str
    status: str
    freshness: str  # fresh / check / redo / unknown, or "not checked"
    age: str  # "as of 3d" when the last check failed and an earlier one stands; else ""
    why: tuple[str, ...]  # what changed, one sentence a line
    platforms: tuple[PlatformBadge, ...]
    best: str  # the best rating on any platform; "no data" when none
    last_posted: date | None
    last_url: str  # https:// only; "" otherwise
    rerecord_first: bool  # redo and strong: the clip most worth re-recording


@dataclass(frozen=True)
class ClipsPanel:
    rows: tuple[ClipRow, ...]
    statuses: tuple[tuple[str, int], ...]  # (status, count) for each status in use, in workflow order
    covered: tuple[str, ...]  # demos with a current clip
    missing: tuple[str, ...]  # demos without one


def build(
    clips: list[dict], checks: dict[str, dict], performance: dict[str, ClipPerformance], now: datetime
) -> ClipsPanel:
    """The panel from plain rows: the clips (as the store lists them), their stored freshness
    checks by id, and their performance by id."""
    rows = []
    for clip in registry.ordered(clips, checks, performance):
        check = checks.get(clip["id"])
        current = freshness.current(clip, check)
        perf = performance.get(clip["id"])
        last = perf.last if perf else None
        rows.append(
            ClipRow(
                id=clip["id"],
                title=clip["title"] or "",
                demo=clip["demo"] or "",
                format=clip["format"] or "",
                status=clip["status"],
                freshness=current["state"] if current else NOT_CHECKED,
                age=f"as of {freshness.ago(current['checked_at'], now)}"
                if current and current["failed_at"] and current["checked_at"]
                else "",
                why=tuple(freshness.explain(clip, check, now)),
                platforms=tuple(
                    PlatformBadge(platform_label(p.platform), p.rating.label, p.measure, p.value, p.detail())
                    for p in (perf.ordered() if perf else [])
                ),
                best=registry.best_rating(clip, performance),
                last_posted=last.last_at.date() if last and last.last_at else None,
                last_url=last.last_url if last and last.last_url.startswith("https://") else "",
                rerecord_first=registry.rerecord_first(clip, check, performance),
            )
        )
    counts = Counter(c["status"] for c in clips)
    covered, missing = registry.coverage(clips)
    return ClipsPanel(
        rows=tuple(rows),
        statuses=tuple((s, counts[s]) for s in CLIP_STATUSES if counts[s]),
        covered=tuple(covered),
        missing=tuple(missing),
    )


def load(store: Store, metrics_file: Path, settings: PerformanceSettings, now: datetime | None = None) -> ClipsPanel:
    """The panel for this store. metrics.sqlite is only read (a missing one is no data)."""
    now = now or datetime.now(UTC)
    clips = store.clips()
    if not clips:
        return ClipsPanel((), (), (), ())
    performance = clip_performance(store, metrics_file, settings, now)
    return build(clips, store.clip_freshness(), performance, now)
