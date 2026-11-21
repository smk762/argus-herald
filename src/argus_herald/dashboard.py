"""The numbers behind /dashboard: do announcements move adoption, and which platform and format best?

Everything is read from metrics.sqlite (read-only) on this machine. Each function returns plain
rows, so the page, its charts and the CSV exports always show the same figures.

The deltas are associations, not effects: counts are small and posts overlap. A broadcast's
window is the day it went out and the two after (0-72h). It's compared with the median day of
the 14 days before. A window that another post's window overlaps is flagged, and the page
greys it out.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import median

WINDOW_DAYS = 3  # the day of the post and the two after: 0-72h
BASELINE_DAYS = 14
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Fixed colour slots (1-8) per entity, so a platform has the same colour on every chart. Slots
# 1-5 are shared by broadcasts and referrers; 8 is "other" on both.
PLATFORMS = [
    ("twitter", "X", 1),
    ("bluesky", "Bluesky", 2),
    ("mastodon", "Mastodon", 3),
    ("reddit", "Reddit", 4),
    ("hn", "Hacker News", 5),
    ("discord", "Discord", 6),
    ("telegram", "Telegram", 7),
    ("other", "Other", 8),
]
REFERRER_GROUPS = [
    ("twitter", "X", 1),
    ("bluesky", "Bluesky", 2),
    ("mastodon", "Mastodon", 3),
    ("reddit", "Reddit", 4),
    ("hn", "Hacker News", 5),
    ("search", "Search", 6),
    ("site", "Own site", 7),
    ("other", "Other", 8),
]
_PLATFORM_ALIASES = {
    "x": "twitter",
    "twitter": "twitter",
    "bsky": "bluesky",
    "bluesky": "bluesky",
    "mastodon": "mastodon",
    "reddit": "reddit",
    "hn": "hn",
    "hackernews": "hn",
    "discord": "discord",
    "telegram": "telegram",
}
_MASTODON_HINTS = ("mastodon", "mstdn", "fosstodon", "mapstodon", "hachyderm", "infosec.exchange", "toot")
_SEARCH = (
    "google.",
    "bing.com",
    "duckduckgo.com",
    "search.brave.com",
    "ecosia.org",
    "kagi.com",
    "yandex.",
    "baidu.com",
)


def platform_key(name: str) -> str:
    return _PLATFORM_ALIASES.get(name.lower().replace(" ", "").replace("_", ""), "other")


def row_platform(name: str) -> str:
    """A platform's key for per-platform rows: the dashboard's key, or its own name when it has none.

    Posts and follower counts both use it, so an unmapped platform's rows meet in one place."""
    key = platform_key(name)
    return name if key == "other" and name else key


def platform_slot(name: str) -> int:
    key = platform_key(name)
    return next(slot for k, _label, slot in PLATFORMS if k == key)


def platform_label(name: str) -> str:
    key = platform_key(name)
    if key == "other":
        return name.capitalize() if name else "Other"
    return next(label for k, label, _slot in PLATFORMS if k == key)


def referrer_group(referrer: str, own_hosts: tuple[str, ...] = ()) -> str:
    host = referrer.lower().strip()
    if host in ("t.co", "x.com", "twitter.com", "mobile.twitter.com") or host.endswith(".x.com"):
        return "twitter"
    if host in ("bsky.app", "bsky.social") or host.endswith(".bsky.app"):
        return "bluesky"
    if any(h in host for h in _MASTODON_HINTS):
        return "mastodon"
    if host in ("reddit.com", "redd.it") or host.endswith(".reddit.com"):
        return "reddit"
    if host == "news.ycombinator.com":
        return "hn"
    if any(host == h or host.endswith("." + h) for h in own_hosts):
        return "site"
    if any(s in host for s in _SEARCH) or host in ("google", "bing", "duckduckgo"):
        return "search"
    return "other"


# ---- reading -----------------------------------------------------------------------------


def connect(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    # as_uri percent-encodes the path: a "?", "#" or "%" in a folder name would otherwise change which file opens
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _parse(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


@dataclass
class Filters:
    days: int = 90
    project: str = ""
    week_start: int = 0  # 0 = Monday, so weeks line up with digest windows
    end: date = field(default_factory=lambda: datetime.now(UTC).date())

    @property
    def start(self) -> date:
        return self.end - timedelta(days=self.days - 1)

    def dates(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.days)]


def projects(conn) -> list[str]:
    if conn is None:
        return []
    have = _tables(conn)
    names: set[str] = set()
    for table, column in (("gh_traffic", "repo"), ("gh_star_counts", "repo"), ("pypi_downloads", "package")):
        if table in have:
            names.update(r[0] for r in conn.execute(f"SELECT DISTINCT {column} FROM {table}"))
    return sorted(names)


# ---- timeline ----------------------------------------------------------------------------

SERIES = [
    ("downloads", "PyPI downloads"),
    ("views", "GitHub views"),
    ("uniques", "GitHub unique visitors"),
    ("clones", "GitHub clones"),
    ("stars", "New GitHub stars"),
    ("visits", "Site visits"),
]


def _daily(conn, sql: str, params: tuple, dates: list[date]) -> list[float | None]:
    """Values per day. Days before the first recorded day are None (not collected), later gaps 0."""
    rows = {str(r[0]): float(r[1] or 0) for r in conn.execute(sql, params)}
    if not rows:
        return [None] * len(dates)
    first = min(rows)
    return [rows.get(d.isoformat(), 0.0) if d.isoformat() >= first else None for d in dates]


def timeline(conn, f: Filters) -> dict[str, list[float | None]]:
    dates = f.dates()
    out: dict[str, list[float | None]] = {key: [None] * len(dates) for key, _ in SERIES}
    if conn is None:
        return out
    have = _tables(conn)
    where, params = ("AND repo = ?", (f.project,)) if f.project else ("", ())
    span = (f.start.isoformat(), f.end.isoformat())
    if "pypi_downloads" in have:
        pw, pp = ("AND package = ?", (f.project,)) if f.project else ("", ())
        out["downloads"] = _daily(
            conn,
            f"SELECT date, SUM(downloads) FROM pypi_downloads WHERE date BETWEEN ? AND ? {pw} GROUP BY date",
            span + pp,
            dates,
        )
    if "gh_traffic" in have:
        for key, column in (("views", "views"), ("uniques", "uniques"), ("clones", "clones")):
            out[key] = _daily(
                conn,
                f"SELECT date, SUM({column}) FROM gh_traffic WHERE date BETWEEN ? AND ? {where} GROUP BY date",
                span + params,
                dates,
            )
    out["stars"] = _new_stars(conn, have, f, dates)
    if "site_traffic" in have and not f.project:
        out["visits"] = _daily(
            conn, "SELECT date, SUM(visits) FROM site_traffic WHERE date BETWEEN ? AND ? GROUP BY date", span, dates
        )
    return out


def _new_stars(conn, have: set[str], f: Filters, dates: list[date]) -> list[float | None]:
    """Stars given per day: from star timestamps where GitHub lists them, else daily count changes."""
    per_day: dict[str, float] = defaultdict(float)
    first: str | None = None
    repos_with_history: set[str] = set()
    if "gh_stars" in have:
        sql = "SELECT repo, substr(starred_at, 1, 10) AS day, COUNT(*) FROM gh_stars"
        sql += " WHERE repo = ? GROUP BY repo, day" if f.project else " GROUP BY repo, day"
        for repo, day, count in conn.execute(sql, (f.project,) if f.project else ()):
            repos_with_history.add(repo)
            per_day[day] += count
            first = min(first or day, day)
    if "gh_star_counts" in have:
        sql = (
            "SELECT repo, date, stars FROM gh_star_counts"
            + (" WHERE repo = ?" if f.project else "")
            + " ORDER BY repo, date"
        )
        last: dict[str, int] = {}
        for repo, day, stars in conn.execute(sql, (f.project,) if f.project else ()):
            if repo in repos_with_history:
                continue
            if repo in last:
                per_day[day] += max(0, stars - last[repo])
            last[repo] = stars
            first = min(first or day, day)
    if first is None:
        return [None] * len(dates)
    return [per_day.get(d.isoformat(), 0.0) if d.isoformat() >= first else None for d in dates]


def rolling_mean(values: list[float | None], window: int = 7) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        chunk = [v for v in values[max(0, i - window + 1) : i + 1] if v is not None]
        out.append(sum(chunk) / len(chunk) if len(chunk) >= min(window, 3) and values[i] is not None else None)
    return out


def followers(conn, f: Filters) -> dict[str, list[float | None]]:
    """Followers per platform per day; a missing day carries the last count forward."""
    if conn is None or "account_metrics" not in _tables(conn):
        return {}
    series: dict[str, dict[str, float]] = defaultdict(dict)
    for row in conn.execute(
        "SELECT platform, date, followers FROM account_metrics WHERE followers IS NOT NULL AND date <= ? ORDER BY date",
        (f.end.isoformat(),),
    ):
        series[row_platform(row["platform"])][row["date"]] = row["followers"]
    out = {}
    for platform, by_day in series.items():
        values, last = [], None
        for d in f.dates():
            key = d.isoformat()
            earlier = [k for k in by_day if k <= key]
            last = by_day[max(earlier)] if earlier else None
            values.append(last)
        if any(v is not None for v in values):
            out[platform] = values
    return out


# ---- markers -----------------------------------------------------------------------------


@dataclass
class Broadcast:
    platform: str
    at: datetime
    url: str
    project: str
    kind: str
    subject: str
    text: str
    logged: bool
    drafted_by: str
    post: str
    media: int
    overlap: bool = False
    engagement_24h: int | None = None
    engagement_7d: int | None = None
    impressions: int | None = None
    deltas: dict[str, float | None] = field(default_factory=dict)
    baselines: dict[str, float | None] = field(default_factory=dict)

    @property
    def link(self) -> str | None:
        return self.url if self.url.startswith("https://") else None

    @property
    def format(self) -> str:
        return self.kind or "release"


def releases(conn, f: Filters) -> list[dict]:
    if conn is None or "events" not in _tables(conn):
        return []
    sql = "SELECT project, version, at, url FROM events WHERE kind = 'release' AND substr(at, 1, 10) BETWEEN ? AND ?"
    params: tuple = (f.start.isoformat(), f.end.isoformat())
    if f.project:
        sql += " AND project = ?"
        params += (f.project,)
    return [
        {"project": r["project"], "version": r["version"], "at": _parse(r["at"]), "url": r["url"]}
        for r in conn.execute(sql + " ORDER BY at", params)
    ]


def broadcasts(conn, f: Filters, *, all_time: bool = False) -> list[Broadcast]:
    if conn is None or "events" not in _tables(conn):
        return []
    out = []
    for r in conn.execute("SELECT source, project, at, url, detail FROM events WHERE kind = 'broadcast' ORDER BY at"):
        try:
            detail = json.loads(r["detail"] or "{}")
            at = _parse(r["at"])
        except ValueError:
            continue
        if not isinstance(detail, dict):
            continue
        try:
            media = int(detail.get("media") or 0)
        except (TypeError, ValueError):
            media = 0
        out.append(
            Broadcast(
                platform=r["source"],
                at=at,
                url=r["url"],
                project=r["project"] or "",
                kind=str(detail.get("kind") or "release"),
                subject=str(detail.get("subject") or ""),
                text=str(detail.get("text") or ""),
                logged=detail.get("logged") not in (False, None),
                drafted_by=str(detail.get("drafted_by") or ("logged" if detail.get("logged") else "template")),
                post=str(detail.get("post") or f"{r['project']}|{detail.get('subject')}"),
                media=media,
            )
        )
    _mark_overlaps(out)
    if not all_time:
        out = [b for b in out if f.start <= b.at.date() <= f.end]
    if f.project:
        out = [b for b in out if b.project in ("", f.project)]
    return out


def _mark_overlaps(items: list[Broadcast]) -> None:
    window = timedelta(days=WINDOW_DAYS)
    for a in items:
        a.overlap = any(b.post != a.post and abs(b.at - a.at) < window for b in items)


# ---- broadcast report ---------------------------------------------------------------------

DELTA_METRICS = [("downloads", "Downloads"), ("views", "Views"), ("stars", "Stars"), ("referrers", "Referrer uniques")]


def engagement_score(row) -> int:
    """Likes, reposts, replies and quotes: the engagement every platform reports."""
    return sum(int(row[k] or 0) for k in ("likes", "reposts", "replies", "quotes"))


def _engagement(conn, b: Broadcast) -> None:
    rows = conn.execute(
        "SELECT * FROM post_metrics WHERE platform = ? AND external_id = ? ORDER BY fetched_at", (b.platform, b.url)
    ).fetchall()
    # Compare parsed times, not strings: stored stamps differ in separator, offset and precision.
    timed = []
    for r in rows:
        try:
            timed.append((_parse(r["fetched_at"]), r))
        except (TypeError, ValueError, AttributeError):
            continue
    if not timed:
        return
    timed.sort(key=lambda pair: pair[0])
    # Each checkpoint takes the first sample in its own window: 24h up to 72h, 7d from then on.
    for attr, after, until in (
        ("engagement_24h", timedelta(hours=24), timedelta(hours=72)),
        ("engagement_7d", timedelta(days=7), timedelta(days=3650)),
    ):
        due, late = b.at + after, b.at + until
        sample = next((r for at, r in timed if due <= at < late), None)
        if sample is not None:
            setattr(b, attr, engagement_score(sample))
    latest_at, latest = timed[-1]
    if latest["impressions"] is not None:
        b.impressions = int(latest["impressions"])
    # An X import isn't timed to the checkpoints: its latest sample stands in for 7d once a week has passed.
    if b.engagement_7d is None and latest["source"] == "x-analytics" and latest_at - b.at >= timedelta(days=7):
        b.engagement_7d = engagement_score(latest)


def _event_window(daily: dict[str, float], day: date) -> tuple[float | None, float | None]:
    """(mean of the 0-72h window minus the baseline median, baseline median)."""
    base = [daily.get((day - timedelta(days=i)).isoformat()) for i in range(1, BASELINE_DAYS + 1)]
    base = [v for v in base if v is not None]
    window = [daily.get((day + timedelta(days=i)).isoformat()) for i in range(WINDOW_DAYS)]
    if len(base) < BASELINE_DAYS // 2 or any(v is None for v in window):
        return None, None
    mid = median(base)
    return sum(window) / WINDOW_DAYS - mid, mid


def _referrer_series(conn, project: str, own_hosts: tuple[str, ...]) -> dict[str, dict[str, float]]:
    """Per group: snapshot date -> uniques over GitHub's trailing 14 days."""
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    if "gh_referrers" not in _tables(conn):
        return out
    sql = "SELECT snapshot_date, referrer, SUM(uniques) FROM gh_referrers"
    sql += " WHERE repo = ? GROUP BY snapshot_date, referrer" if project else " GROUP BY snapshot_date, referrer"
    for day, referrer, uniques in conn.execute(sql, (project,) if project else ()):
        out[referrer_group(referrer, own_hosts)][day] += uniques
    return out


def _snapshot_change(series: dict[str, float], day: date) -> float | None:
    before = [k for k in series if k < day.isoformat()]
    after = [k for k in series if k >= (day + timedelta(days=WINDOW_DAYS)).isoformat()]
    if not before or not after:
        return None
    return series[min(after)] - series[max(before)]


def broadcast_report(conn, f: Filters, own_hosts: tuple[str, ...] = ()) -> list[Broadcast]:
    items = broadcasts(conn, f)
    if conn is None or not items:
        return items
    have = _tables(conn)
    cache: dict[str, dict[str, dict[str, float]]] = {}
    year = Filters(days=f.days + 60, project="", end=f.end)

    def daily(project: str) -> dict[str, dict[str, float]]:
        if project not in cache:
            g = Filters(days=year.days, project=project, end=year.end)
            series = timeline(conn, g)
            cache[project] = {
                key: {d.isoformat(): v for d, v in zip(g.dates(), values, strict=False) if v is not None}
                for key, values in series.items()
            }
        return cache[project]

    refs_by_group = _referrer_series(conn, f.project, own_hosts)
    known = set(projects(conn))
    shared = {g for g, _l, _s in REFERRER_GROUPS[:5]}
    for b in items:
        if "post_metrics" in have:
            _engagement(conn, b)
        project = f.project or b.project
        if project and project not in known:
            project = ""
        series = daily(project)
        for key in ("downloads", "views", "stars"):
            b.deltas[key], b.baselines[key] = _event_window(series.get(key, {}), b.at.date())
        group = platform_key(b.platform)
        refs = refs_by_group.get(group) if group in shared else None
        b.deltas["referrers"] = _snapshot_change(refs, b.at.date()) if refs else None
    return items


def filter_report(
    items: list[Broadcast],
    *,
    platform: str = "",
    kind: str = "",
    media: str = "",
    drafted_by: str = "",
    logged: str = "",
) -> list[Broadcast]:
    out = items
    if platform:
        out = [b for b in out if platform_key(b.platform) == platform or b.platform == platform]
    if kind:
        out = [b for b in out if b.format == kind]
    if media in ("yes", "no"):
        out = [b for b in out if (b.media > 0) == (media == "yes")]
    if drafted_by:
        out = [
            b for b in out if (b.drafted_by.split(":")[0] if drafted_by != "template" else b.drafted_by) == drafted_by
        ]
    if logged in ("yes", "no"):
        out = [b for b in out if b.logged == (logged == "yes")]
    return out


def _by_delta(key: str):
    return lambda b: (b.deltas.get(key) is not None, b.deltas.get(key) or 0)


SORT_KEYS = {
    "at": lambda b: b.at,
    "platform": lambda b: b.platform,
    "format": lambda b: b.format,
    "e24": lambda b: (b.engagement_24h is not None, b.engagement_24h or 0),
    "e7": lambda b: (b.engagement_7d is not None, b.engagement_7d or 0),
    **{f"d_{k}": _by_delta(k) for k, _ in DELTA_METRICS},
}


# ---- referrers and platform summary --------------------------------------------------------


def referrers(conn, f: Filters, own_hosts: tuple[str, ...] = ()) -> tuple[list[str], dict[str, list[float]]]:
    """Snapshot dates in range, and uniques per group on each (GitHub's trailing 14 days)."""
    if conn is None:
        return [], {}
    series = _referrer_series(conn, f.project, own_hosts)
    days = sorted({d for g in series.values() for d in g if f.start.isoformat() <= d <= f.end.isoformat()})
    return days, {
        g: [series.get(g, {}).get(d, 0.0) for d in days]
        for g, _l, _s in REFERRER_GROUPS
        if any(series.get(g, {}).get(d, 0.0) for d in days)
    }


def week_of(day: date, week_start: int) -> date:
    return day - timedelta(days=(day.weekday() - week_start) % 7)


def platform_summary(conn, items: list[Broadcast], f: Filters) -> list[dict]:
    """Per platform per week: posts, engagement, referrer uniques around its posts, follower change."""
    rows: dict[tuple[str, date], dict] = {}
    for b in items:
        key = (row_platform(b.platform), week_of(b.at.date(), f.week_start))
        row = rows.setdefault(
            key,
            {
                "platform": key[0],
                "week": key[1],
                "posts": 0,
                "engagement": 0,
                "engaged_posts": 0,
                "referrers": 0.0,
                "followers": None,
            },
        )
        row["posts"] += 1
        score = b.engagement_7d if b.engagement_7d is not None else b.engagement_24h
        if score is not None:
            row["engagement"] += score
            row["engaged_posts"] += 1
        if b.deltas.get("referrers") and not b.overlap:
            row["referrers"] += b.deltas["referrers"]
    audience = followers(conn, Filters(days=f.days + 7, project="", end=f.end))
    dates = Filters(days=f.days + 7, project="", end=f.end).dates()
    for platform, values in audience.items():
        by_day = dict(zip(dates, values, strict=False))
        weeks = sorted({week_of(d, f.week_start) for d in f.dates()})
        for week in weeks:
            end = min(week + timedelta(days=6), f.end)
            now, before = by_day.get(end), by_day.get(week - timedelta(days=1))
            if now is None or before is None:
                continue
            key = (platform, week)  # followers() already keyed it with row_platform
            row = rows.setdefault(
                key,
                {
                    "platform": key[0],
                    "week": week,
                    "posts": 0,
                    "engagement": 0,
                    "engaged_posts": 0,
                    "referrers": 0.0,
                    "followers": None,
                },
            )
            row["followers"] = now - before
    return sorted(rows.values(), key=lambda r: (r["week"], platform_slot(r["platform"]), r["platform"]), reverse=False)


def summary_tiles(series: dict[str, list[float | None]]) -> list[dict]:
    """Last 7 days against the 7 before, for the headline row."""
    tiles = []
    for key, label in SERIES:
        values = series.get(key) or []
        recent, prior = values[-7:], values[-14:-7]
        if len(recent) < 7 or any(v is None for v in recent):
            continue  # a shorter run would be shown as a week's total
        now = sum(v for v in recent if v is not None)
        full_prior = len(prior) == 7 and all(v is not None for v in prior)
        before = sum(v for v in prior if v is not None) if full_prior else None
        tiles.append({"key": key, "label": label, "value": now, "delta": None if before is None else now - before})
    return tiles
