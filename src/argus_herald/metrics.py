"""Adoption metrics, kept on this machine in their own SQLite file.

Collectors (each optional, each skips cleanly when it can't run):

- GitHub traffic: daily views and clones, referrers and popular paths. GitHub keeps only 14
  days, so this must run at least every 13 days or data is lost. Needs GITHUB_TOKEN with
  read access to the repos' Administration permission (fine-grained token) or push access
  (classic token).
- GitHub stars: a daily count per repo (works without a token), and with GITHUB_TOKEN the time
  each star was given (GitHub only lists stargazers to authenticated requests).
- PyPI downloads per day from pypistats.org (the last 180 days, without mirrors).
- Engagement per post (Bluesky, Mastodon, the X analytics import) and site traffic: engagement.py.
- Events: release times from the PyPI JSON API, and broadcasts from herald's own queue
  (published drafts that have a post URL or id). These are the markers on the dashboard.

Every write is an upsert, so `collect` can run as often as you like.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote

from argus_herald.config import Config
from argus_herald.engagement import collect_posts, collect_site, site_configured
from argus_herald.http import Http, HttpError
from argus_herald.models import CLIP_CURRENT, PUBLISHED, Draft
from argus_herald.pypi import list_versions
from argus_herald.store import Store
from argus_herald.workflow import tracked_projects

GITHUB_API = "https://api.github.com"
PYPISTATS = "https://pypistats.org/api/packages"
# GitHub refuses stargazer pages past 400 (HTTP 422, "pagination is limited for this resource").
STAR_PAGES = 400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gh_traffic (
    repo TEXT NOT NULL, date TEXT NOT NULL,
    views INTEGER, uniques INTEGER, clones INTEGER, clone_uniques INTEGER,
    PRIMARY KEY (repo, date)
);
CREATE TABLE IF NOT EXISTS gh_referrers (
    repo TEXT NOT NULL, snapshot_date TEXT NOT NULL, referrer TEXT NOT NULL,
    count INTEGER NOT NULL, uniques INTEGER NOT NULL,
    PRIMARY KEY (repo, snapshot_date, referrer)
);
CREATE TABLE IF NOT EXISTS gh_paths (
    repo TEXT NOT NULL, snapshot_date TEXT NOT NULL, path TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL, uniques INTEGER NOT NULL,
    PRIMARY KEY (repo, snapshot_date, path)
);
-- One row per star. The stargazer is kept only as a hash, enough to de-duplicate.
CREATE TABLE IF NOT EXISTS gh_stars (
    repo TEXT NOT NULL, user_hash TEXT NOT NULL, starred_at TEXT NOT NULL,
    PRIMARY KEY (repo, user_hash)
);
CREATE TABLE IF NOT EXISTS gh_star_counts (
    repo TEXT NOT NULL, date TEXT NOT NULL, stars INTEGER NOT NULL,
    PRIMARY KEY (repo, date)
);
CREATE TABLE IF NOT EXISTS pypi_downloads (
    package TEXT NOT NULL, date TEXT NOT NULL, downloads INTEGER NOT NULL,
    PRIMARY KEY (package, date)
);
-- kind: release | broadcast. For broadcasts, source is the platform and url the post.
CREATE TABLE IF NOT EXISTS events (
    kind TEXT NOT NULL, source TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '', at TEXT NOT NULL, url TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (kind, source, project, version, url)
);
-- Audience size per platform per day. X rows are entered or imported by hand (no paid API).
CREATE TABLE IF NOT EXISTS account_metrics (
    platform TEXT NOT NULL, account TEXT NOT NULL, date TEXT NOT NULL,
    followers INTEGER, following INTEGER, posts INTEGER, source TEXT NOT NULL DEFAULT 'api',
    PRIMARY KEY (platform, account, date)
);
-- Engagement per post over time: one row per sample (Bluesky and Mastodon at +1h, +24h, +72h,
-- +7d) or per X analytics import. external_id is the broadcast's URL in events.
CREATE TABLE IF NOT EXISTS post_metrics (
    platform TEXT NOT NULL, external_id TEXT NOT NULL, fetched_at TEXT NOT NULL,
    likes INTEGER, reposts INTEGER, replies INTEGER, quotes INTEGER, impressions INTEGER,
    engagements INTEGER, link_clicks INTEGER, profile_visits INTEGER, source TEXT NOT NULL DEFAULT 'api',
    PRIMARY KEY (platform, external_id, fetched_at)
);
-- Account-wide engagement for a day (X analytics export) or a longer period (entered by hand).
CREATE TABLE IF NOT EXISTS account_engagement (
    platform TEXT NOT NULL, account TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
    impressions INTEGER, engagements INTEGER, likes INTEGER, reposts INTEGER, replies INTEGER,
    link_clicks INTEGER, profile_visits INTEGER, new_follows INTEGER, source TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (platform, account, start_date, end_date)
);
-- Site page views (Cloudflare Web Analytics), per day. `host` tells subdomains that share one
-- site tag apart; it is '' on rows collected before hosts were recorded.
CREATE TABLE IF NOT EXISTS site_traffic (
    date TEXT NOT NULL, host TEXT NOT NULL DEFAULT '', path TEXT NOT NULL,
    pageviews INTEGER NOT NULL, visits INTEGER NOT NULL,
    PRIMARY KEY (date, host, path)
);
CREATE TABLE IF NOT EXISTS site_referrers (
    date TEXT NOT NULL, referrer TEXT NOT NULL, pageviews INTEGER NOT NULL, visits INTEGER NOT NULL,
    PRIMARY KEY (date, referrer)
);
CREATE TABLE IF NOT EXISTS runs (
    collector TEXT PRIMARY KEY, ran_at TEXT NOT NULL, detail TEXT NOT NULL
);
"""


def metrics_path(config: Config) -> Path:
    return config.store_path.parent / "metrics.sqlite"


class Metrics:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._add_site_host()
        self.conn.executescript(_SCHEMA)
        self._schema: dict[str, dict[str, int]] = {}

    def _add_site_host(self) -> None:
        """site_traffic was keyed (date, path) before hosts were recorded. The key changes, so the
        table is rebuilt; its rows are kept with a blank host."""
        columns = [row["name"] for row in self.conn.execute("PRAGMA table_info(site_traffic)")]
        if not columns or "host" in columns:
            return
        with self.transaction():
            self.conn.execute("ALTER TABLE site_traffic RENAME TO site_traffic_old")
            self.conn.execute(
                "CREATE TABLE site_traffic (date TEXT NOT NULL, host TEXT NOT NULL DEFAULT '', path TEXT NOT NULL, "
                "pageviews INTEGER NOT NULL, visits INTEGER NOT NULL, PRIMARY KEY (date, host, path))"
            )
            self.conn.execute(
                "INSERT INTO site_traffic (date, path, pageviews, visits) "
                "SELECT date, path, pageviews, visits FROM site_traffic_old"
            )
            self.conn.execute("DROP TABLE site_traffic_old")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN")
        try:
            yield
            self.conn.commit()
        except BaseException:
            # KeyboardInterrupt and cancellation too: an open transaction wedges the connection.
            self.conn.rollback()
            raise

    def _columns(self, table: str, names: Iterable[str]) -> dict[str, int]:
        """The table's columns (name to primary-key position, 0 for none). Table and column names
        go into SQL text, so each must be one the schema has."""
        if table not in self._schema:
            rows = self.conn.execute("SELECT name, pk FROM pragma_table_info(?)", (table,)).fetchall()
            if not rows:
                raise ValueError(f"metrics has no table {table!r}")
            self._schema[table] = {str(r["name"]): int(r["pk"]) for r in rows}
        known = self._schema[table]
        unknown = [n for n in names if n not in known]
        if unknown:
            raise ValueError(f"metrics table {table} has no column {', '.join(map(repr, unknown))}")
        return known

    def upsert(self, table: str, rows: list[dict]) -> int:
        """Insert rows, or update the columns they carry where the primary key is already stored.
        Columns a row leaves out keep their stored value (or the default, for a new row)."""
        if not rows:
            return 0
        columns = list(rows[0])
        known = self._columns(table, columns)
        if any(row.keys() != rows[0].keys() for row in rows):
            raise ValueError(f"rows for {table} don't all have the same columns")
        target = ", ".join(f'"{name}"' for _, name in sorted((pk, name) for name, pk in known.items() if pk))
        updates = [c for c in columns if not known[c]]
        action = "DO UPDATE SET " + ", ".join(f'"{c}" = excluded."{c}"' for c in updates) if updates else "DO NOTHING"
        names = ", ".join(f'"{c}"' for c in columns)
        marks = ", ".join("?" for _ in columns)
        with self.transaction():
            self.conn.executemany(
                f'INSERT INTO "{table}" ({names}) VALUES ({marks}) ON CONFLICT ({target}) {action}',
                [tuple(row[c] for c in columns) for row in rows],
            )
        return len(rows)

    def count(self, table: str, **where: object) -> int:
        self._columns(table, where)
        clause = " AND ".join(f'"{k}" = ?' for k in where) or "1"
        return int(
            self.conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {clause}', tuple(where.values())).fetchone()[0]
        )

    def record_run(self, collector: str, detail: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (collector, ran_at, detail) VALUES (?, ?, ?)",
            (collector, _now(), detail),
        )


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def collect(
    config: Config, store: Store, metrics: Metrics, http: Http, env: Mapping[str, str], *, since: date | None = None
) -> Report:
    report = Report()
    token = (env.get("GITHUB_TOKEN") or "").strip()
    owner, prefix = config.discover_owner, config.discover_prefix
    repos: list[str] = []
    if owner:
        from argus_herald.discover import github_repo_info

        try:
            info = [r for r in github_repo_info(http, owner, token) if str(r["name"]).startswith(prefix)]
        except (HttpError, OSError, ValueError) as exc:
            report.errors.append(f"GitHub repo list: {exc}")
            info = []
        repos = sorted(str(r["name"]) for r in info)
        if info:
            today = date.today().isoformat()
            metrics.upsert(
                "gh_star_counts",
                [{"repo": str(r["name"]), "date": today, "stars": int(r.get("stargazers_count") or 0)} for r in info],
            )
            metrics.record_run("gh_star_counts", f"{len(info)} repos")
            report.lines.append(
                f"gh_star_counts: {len(info)} repos, {sum(int(r.get('stargazers_count') or 0) for r in info)} stars"
            )
    else:
        report.skipped.append("GitHub: set pypi.discover.github_owner (and prefix) to collect repo metrics")

    if repos:
        if token:
            denied: list[str] = []
            _run(report, metrics, "gh_traffic", lambda: collect_traffic(metrics, http, owner, repos, token, denied))
            if denied:
                report.errors.append(
                    f"gh_traffic: the token can't read traffic for {', '.join(denied)}. Add them to the token's "
                    "repositories; GitHub keeps traffic for 14 days only"
                )
            try:
                _run(
                    report,
                    metrics,
                    "gh_stars",
                    lambda: collect_stars(metrics, http, owner, repos, token),
                    reraise_denied=True,
                )
            except HttpError as exc:
                report.skipped.append(
                    f"GitHub star history: HTTP {exc.status} (GitHub limits stargazer lists to admins and collaborators; "
                    "daily star counts are still kept)"
                )
        else:
            report.skipped.append("GitHub traffic, referrers, paths and star history: GITHUB_TOKEN is not set")
    _accounts(report, metrics, http, config, env)
    projects = tracked_projects(config)
    _run(report, metrics, "pypi_downloads", lambda: collect_downloads(metrics, http, projects, since))
    _run(report, metrics, "events", lambda: collect_events(metrics, store, http, projects))
    posts = collect_posts(metrics, http)
    report.lines += posts.lines
    report.skipped += posts.skipped
    report.errors += posts.errors
    missing = site_configured(env)
    if 0 < len(missing) < 3:  # half set up; with none set it's simply not in use (platforms shows it)
        report.skipped.append(f"site traffic: {', '.join(missing)} not set")
    elif not missing:
        _run(report, metrics, "site_traffic", lambda: collect_site(metrics, http, env))
    _clip_freshness(report, config, store, http, token)
    return report


def _clip_freshness(report: Report, config: Config, store: Store, http: Http, token: str) -> None:
    """Compare recorded clips with their demos. A clip whose check fails keeps its last state."""
    from argus_herald.freshness import refresh

    if not any(clip["status"] in CLIP_CURRENT for clip in store.clips()):
        return
    try:
        summary = refresh(store, http, config, token)
    except (sqlite3.Error, OSError, ValueError, KeyError) as exc:  # the rest of collect has run; say so and stop
        report.errors.append(f"clip_freshness: {exc}")
        return
    report.lines.append(f"clip_freshness: {summary.line}")
    report.errors += [f"clip_freshness: {line}" for line in summary.errors]


def collect_engagement(store: Store, metrics: Metrics, http: Http) -> Report:
    """The hourly part: refresh broadcasts from the queue, then sample posts at their checkpoints."""
    report = Report()
    _run(report, metrics, "broadcasts", lambda: sync_broadcasts(metrics, store))
    posts = collect_posts(metrics, http)
    report.lines += posts.lines
    report.skipped += posts.skipped
    report.errors += posts.errors
    return report


def _run(report: Report, metrics: Metrics, name: str, job, *, reraise_denied: bool = False) -> None:
    try:
        detail = job()
    except HttpError as exc:
        if reraise_denied and exc.status in (401, 403):
            raise
        report.errors.append(f"{name}: {exc}")
        return
    except (OSError, ValueError, KeyError) as exc:
        report.errors.append(f"{name}: {exc}")
        return
    metrics.record_run(name, detail)
    report.lines.append(f"{name}: {detail}")


def _gh_headers(token: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_json(http: Http, url: str, headers: Mapping[str, str]):
    status, body = http.request("GET", url, headers=dict(headers))
    if status != 200:
        raise HttpError("GET", url, status)
    return json.loads(body.decode("utf-8"))


def collect_traffic(
    metrics: Metrics, http: Http, owner: str, repos: list[str], token: str, denied: list[str] | None = None
) -> str:
    """Traffic for every repo the token can read. A repo it can't (403 or 404: a fine-grained token
    that doesn't list it) is added to `denied` and the rest are still collected, because GitHub keeps
    only 14 days and one new repo must not cost the others theirs. Refused for all of them, it raises."""
    headers = _gh_headers(token)
    today = date.today().isoformat()
    days = refs = paths = 0
    refused: list[str] = []
    for repo in repos:
        base = f"{GITHUB_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/traffic"
        try:
            views = _get_json(http, f"{base}/views?per=day", headers)
        except HttpError as exc:
            if exc.status not in (403, 404):
                raise
            refused.append(repo)
            if len(refused) == len(repos):
                raise  # not one repo answered: the token itself is the problem
            continue
        clones = _get_json(http, f"{base}/clones?per=day", headers)
        by_day: dict[str, dict] = {}
        for item in views.get("views") or []:
            day = str(item["timestamp"])[:10]
            by_day.setdefault(
                day, {"repo": repo, "date": day, "views": 0, "uniques": 0, "clones": 0, "clone_uniques": 0}
            )
            by_day[day].update(views=int(item["count"]), uniques=int(item["uniques"]))
        for item in clones.get("clones") or []:
            day = str(item["timestamp"])[:10]
            by_day.setdefault(
                day, {"repo": repo, "date": day, "views": 0, "uniques": 0, "clones": 0, "clone_uniques": 0}
            )
            by_day[day].update(clones=int(item["count"]), clone_uniques=int(item["uniques"]))
        days += metrics.upsert("gh_traffic", list(by_day.values()))
        refs += metrics.upsert(
            "gh_referrers",
            [
                {
                    "repo": repo,
                    "snapshot_date": today,
                    "referrer": str(r["referrer"]),
                    "count": int(r["count"]),
                    "uniques": int(r["uniques"]),
                }
                for r in _get_json(http, f"{base}/popular/referrers", headers)
            ],
        )
        paths += metrics.upsert(
            "gh_paths",
            [
                {
                    "repo": repo,
                    "snapshot_date": today,
                    "path": str(p["path"]),
                    "title": str(p.get("title") or ""),
                    "count": int(p["count"]),
                    "uniques": int(p["uniques"]),
                }
                for p in _get_json(http, f"{base}/popular/paths", headers)
            ],
        )
    if denied is not None:
        denied.extend(refused)
    return f"{len(repos) - len(refused)} repos, {days} repo-days, {refs} referrer rows, {paths} path rows"


def collect_stars(metrics: Metrics, http: Http, owner: str, repos: list[str], token: str) -> str:
    """Stars with timestamps. After the first run, starts near the page the known stars end on."""
    headers = _gh_headers(token, "application/vnd.github.star+json")
    added = 0

    def fetch(repo: str, page: int) -> tuple[int, int, int]:
        """Store one page: (items on the page, stars on it, stars that were new)."""
        url = f"{GITHUB_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/stargazers?per_page=100&page={page}"
        batch = _get_json(http, url, headers)
        rows = [
            {
                "repo": repo,
                "user_hash": _hash((item.get("user") or {}).get("login") or ""),
                "starred_at": str(item.get("starred_at") or ""),
            }
            for item in batch
            if isinstance(item, dict) and item.get("starred_at")
        ]
        before = metrics.count("gh_stars", repo=repo)
        metrics.upsert("gh_stars", rows)
        return len(batch), len(rows), metrics.count("gh_stars", repo=repo) - before

    capped: list[str] = []
    for repo in repos:
        known = metrics.count("gh_stars", repo=repo)
        # Local rows are never deleted, but an unstar shortens GitHub's list, so new stars can sit
        # on an earlier page than the local count says. Start a page early, and walk back until a
        # page holds a star already known (or page 1).
        page = min(max(1, known // 100), STAR_PAGES)
        size, stars, new = fetch(repo, page)
        added += new
        back = page - 1
        while back >= 1 and new == stars:
            _, stars, new = fetch(repo, back)
            added += new
            back -= 1
        while size >= 100 and page < STAR_PAGES:
            page += 1
            size, _, new = fetch(repo, page)
            added += new
        if size >= 100:  # a full last page: GitHub has stars past the pages it lists
            capped.append(repo)
    total = int(metrics.conn.execute("SELECT COUNT(*) FROM gh_stars").fetchone()[0])
    detail = f"{len(repos)} repos, {total} stars ({added} new)"
    if capped:
        detail += (
            f"; GitHub lists only the first {STAR_PAGES * 100} stargazers, so newer stars on "
            f"{', '.join(capped)} aren't recorded (daily star counts are still kept)"
        )
    return detail


def collect_downloads(metrics: Metrics, http: Http, packages: list[str], since: date | None) -> str:
    rows: list[dict] = []
    for package in packages:
        data = _get_json(
            http, f"{PYPISTATS}/{quote(package.lower(), safe='')}/overall?mirrors=false", {"Accept": "application/json"}
        )
        for item in data.get("data") or []:
            if item.get("category") != "without_mirrors":
                continue
            day = str(item["date"])
            if since and day < since.isoformat():
                continue
            rows.append({"package": package, "date": day, "downloads": int(item["downloads"])})
    metrics.upsert("pypi_downloads", rows)
    return f"{len(packages)} packages, {len(rows)} package-days"


def collect_events(metrics: Metrics, store: Store, http: Http, packages: list[str]) -> str:
    rows: list[dict] = []
    for package in packages:
        for ref in list_versions(http, package):
            if ref.upload_time:
                rows.append(
                    {
                        "kind": "release",
                        "source": "pypi",
                        "project": package,
                        "version": ref.version,
                        "at": ref.upload_time.replace("Z", "+00:00"),
                        "url": f"https://pypi.org/project/{package}/{ref.version}/",
                        "detail": "",
                    }
                )
    metrics.upsert("events", rows)
    return f"{len(rows)} releases, {sync_broadcasts(metrics, store)}"


@dataclass(frozen=True)
class PostRef:
    """A post that went out, keyed the way metrics.sqlite knows it: `platform` and `url` are a
    broadcast's events.source and events.url, and a sample's post_metrics.platform and external_id."""

    via: str  # "draft" (published, or handed off and confirmed) or "logged" (posted outside herald)
    id: int
    platform: str
    url: str  # the post URL or id; "draft:<id>" for a published draft that has none
    at: str


def posts_out(store: Store) -> list[tuple[PostRef, Draft | sqlite3.Row]]:
    """Every post that went out, with the draft or logged row it came from."""
    out: list[tuple[PostRef, Draft | sqlite3.Row]] = []
    for draft in store.list_drafts(status=PUBLISHED):
        url = draft.external_id or f"draft:{draft.id}"
        out.append((PostRef("draft", draft.id, draft.platform, url, draft.updated_at), draft))
    for row in store.logged_broadcasts():
        out.append((PostRef("logged", int(row["id"]), row["platform"], row["url"], row["at"]), row))
    return out


def sync_broadcasts(metrics: Metrics, store: Store) -> str:
    broadcasts: list[dict] = []
    posts = posts_out(store)
    for ref, draft in posts:
        if ref.via != "draft":
            continue
        broadcasts.append(
            {
                "kind": "broadcast",
                "source": ref.platform,
                "project": draft.project,
                "version": draft.version,
                "at": ref.at,
                "url": ref.url,
                "detail": json.dumps(
                    {
                        "draft": draft.id,
                        "kind": draft.kind,
                        "subject": draft.label,
                        "text": draft.body[:500],
                        "logged": False,
                        "drafted_by": draft.drafted_by,
                        "post": draft.post_key,
                        "media": len(store.media(draft.id)),
                    }
                ),
            }
        )
    for ref, row in posts:
        if ref.via != "logged":
            continue
        broadcasts.append(
            {
                "kind": "broadcast",
                "source": ref.platform,
                "project": row["project"],
                "version": "",
                "at": ref.at,
                "url": ref.url,
                "detail": json.dumps(
                    {
                        "logged": row["id"],
                        "kind": row["kind"],
                        "subject": row["subject"],
                        "text": row["text"][:500],
                        "post": f"logged:{row['id']}",
                        "media": 0,
                    }
                ),
            }
        )
    # Broadcasts are rebuilt from their sources, so edits and deletions carry through.
    with metrics.transaction():
        metrics.conn.execute("DELETE FROM events WHERE kind = 'broadcast'")
        metrics.conn.executemany(
            "INSERT OR REPLACE INTO events (kind, source, project, version, at, url, detail) "
            "VALUES (:kind, :source, :project, :version, :at, :url, :detail)",
            broadcasts,
        )
    logged = sum(1 for b in broadcasts if '"logged": false' not in b["detail"])
    return f"{len(broadcasts)} broadcasts ({logged} logged by hand)"


def _accounts(report: Report, metrics: Metrics, http: Http, config: Config, env: Mapping[str, str]) -> None:
    accounts = config.accounts
    jobs = []
    if accounts.bluesky:
        jobs.append(("bluesky", accounts.bluesky, lambda: bluesky_counts(http, accounts.bluesky)))
    if accounts.mastodon:
        jobs.append(("mastodon", accounts.mastodon, lambda: mastodon_counts(http, *accounts.mastodon_parts)))
    key = (env.get("YOUTUBE_API_KEY") or "").strip()
    if accounts.youtube and key:
        jobs.append(("youtube", accounts.youtube, lambda: youtube_counts(http, accounts.youtube, key)))
    elif accounts.youtube:
        report.skipped.append("YouTube subscribers: YOUTUBE_API_KEY is not set")
    today = date.today().isoformat()
    for platform, account, fetch in jobs:
        try:
            counts = fetch()
        except HttpError as exc:
            if exc.status in (400, 404):
                report.skipped.append(f"{platform} account {account}: not found (yet)")
            else:
                report.errors.append(f"account_metrics {platform}: {exc}")
            continue
        except (OSError, ValueError, KeyError) as exc:
            report.errors.append(f"account_metrics {platform}: {exc}")
            continue
        metrics.upsert(
            "account_metrics", [{"platform": platform, "account": account, "date": today, **counts, "source": "api"}]
        )
        report.lines.append(f"account_metrics {platform}: {counts['followers']} followers")


def bluesky_counts(http: Http, handle: str) -> dict:
    url = f"https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor={quote(handle, safe='')}"
    data = _get_json(http, url, {"Accept": "application/json"})
    return {
        "followers": int(data["followersCount"]),
        "following": int(data.get("followsCount") or 0),
        "posts": int(data.get("postsCount") or 0),
    }


def mastodon_counts(http: Http, base: str, user: str) -> dict:
    url = f"{base}/api/v1/accounts/lookup?acct={quote(user, safe='')}"
    data = _get_json(http, url, {"Accept": "application/json"})
    return {
        "followers": int(data["followers_count"]),
        "following": int(data.get("following_count") or 0),
        "posts": int(data.get("statuses_count") or 0),
    }


def youtube_counts(http: Http, channel_id: str, key: str) -> dict:
    url = (
        f"https://www.googleapis.com/youtube/v3/channels?part=statistics&id={quote(channel_id, safe='')}"
        f"&key={quote(key, safe='')}"
    )
    data = _get_json(http, url, {"Accept": "application/json"})
    items = data.get("items") or []
    if not items:
        raise HttpError("GET", "youtube channels", 404)
    stats = items[0].get("statistics") or {}
    hidden = stats.get("hiddenSubscriberCount")
    return {
        "followers": None if hidden else int(stats.get("subscriberCount") or 0),
        "following": None,
        "posts": int(stats.get("videoCount") or 0),
    }


def record_account(
    metrics: Metrics,
    platform: str,
    account: str,
    day: str,
    followers: int | None,
    following: int | None = None,
    posts: int | None = None,
) -> None:
    """A row entered by hand (X has no free API for this)."""
    metrics.upsert(
        "account_metrics",
        [
            {
                "platform": platform,
                "account": account,
                "date": day,
                "followers": followers,
                "following": following,
                "posts": posts,
                "source": "manual",
            }
        ],
    )


def _hash(login: str) -> str:
    return hashlib.sha256(login.lower().encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
