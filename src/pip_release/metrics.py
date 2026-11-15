"""Adoption metrics, kept on this machine in their own SQLite file.

Collectors (each optional, each skips cleanly when it can't run):

- GitHub traffic: daily views and clones, referrers and popular paths. GitHub keeps only 14
  days, so this must run at least every 13 days or data is lost. Needs GITHUB_TOKEN with
  read access to the repos' Administration permission (fine-grained token) or push access
  (classic token).
- GitHub stars: a daily count per repo (works without a token), and with GITHUB_TOKEN the time
  each star was given (GitHub only lists stargazers to authenticated requests).
- PyPI downloads per day from pypistats.org (the last 180 days, without mirrors).
- Events: release times from the PyPI JSON API, and broadcasts from herald's own queue
  (published drafts that have a post URL or id). These are the markers on the dashboard.

Every write is an upsert, so `collect` can run as often as you like.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

from pip_release.config import Config
from pip_release.http import Http, HttpError
from pip_release.models import PUBLISHED
from pip_release.pypi import list_versions
from pip_release.store import Store
from pip_release.workflow import tracked_projects

GITHUB_API = "https://api.github.com"
PYPISTATS = "https://pypistats.org/api/packages"

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
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN")
        try:
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def upsert(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        columns = list(rows[0])
        marks = ", ".join("?" for _ in columns)
        with self.transaction():
            self.conn.executemany(
                f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({marks})",
                [tuple(row[c] for c in columns) for row in rows],
            )
        return len(rows)

    def count(self, table: str, **where: object) -> int:
        clause = " AND ".join(f"{k} = ?" for k in where) or "1"
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {clause}", tuple(where.values())).fetchone()[0])

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
        from pip_release.discover import github_repo_info

        try:
            info = [r for r in github_repo_info(http, owner, token) if str(r["name"]).startswith(prefix)]
        except (HttpError, OSError, ValueError) as exc:
            report.errors.append(f"GitHub repo list: {exc}")
            info = []
        repos = sorted(str(r["name"]) for r in info)
        if info:
            today = date.today().isoformat()
            metrics.upsert("gh_star_counts", [
                {"repo": str(r["name"]), "date": today, "stars": int(r.get("stargazers_count") or 0)} for r in info
            ])
            metrics.record_run("gh_star_counts", f"{len(info)} repos")
            report.lines.append(f"gh_star_counts: {len(info)} repos, {sum(int(r.get('stargazers_count') or 0) for r in info)} stars")
    else:
        report.skipped.append("GitHub: set pypi.discover.github_owner (and prefix) to collect repo metrics")

    if repos:
        if token:
            _run(report, metrics, "gh_traffic", lambda: collect_traffic(metrics, http, owner, repos, token))
            try:
                _run(report, metrics, "gh_stars", lambda: collect_stars(metrics, http, owner, repos, token), reraise_denied=True)
            except HttpError as exc:
                report.skipped.append(
                    f"GitHub star history: HTTP {exc.status} (GitHub limits stargazer lists to admins and collaborators; "
                    "daily star counts are still kept)"
                )
        else:
            report.skipped.append("GitHub traffic, referrers, paths and star history: GITHUB_TOKEN is not set")
    projects = tracked_projects(config)
    _run(report, metrics, "pypi_downloads", lambda: collect_downloads(metrics, http, projects, since))
    _run(report, metrics, "events", lambda: collect_events(metrics, store, http, projects))
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


def collect_traffic(metrics: Metrics, http: Http, owner: str, repos: list[str], token: str) -> str:
    headers = _gh_headers(token)
    today = date.today().isoformat()
    days = refs = paths = 0
    for repo in repos:
        base = f"{GITHUB_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/traffic"
        views = _get_json(http, f"{base}/views?per=day", headers)
        clones = _get_json(http, f"{base}/clones?per=day", headers)
        by_day: dict[str, dict] = {}
        for item in views.get("views") or []:
            day = str(item["timestamp"])[:10]
            by_day.setdefault(day, {"repo": repo, "date": day, "views": 0, "uniques": 0, "clones": 0, "clone_uniques": 0})
            by_day[day].update(views=int(item["count"]), uniques=int(item["uniques"]))
        for item in clones.get("clones") or []:
            day = str(item["timestamp"])[:10]
            by_day.setdefault(day, {"repo": repo, "date": day, "views": 0, "uniques": 0, "clones": 0, "clone_uniques": 0})
            by_day[day].update(clones=int(item["count"]), clone_uniques=int(item["uniques"]))
        days += metrics.upsert("gh_traffic", list(by_day.values()))
        refs += metrics.upsert("gh_referrers", [
            {"repo": repo, "snapshot_date": today, "referrer": str(r["referrer"]), "count": int(r["count"]), "uniques": int(r["uniques"])}
            for r in _get_json(http, f"{base}/popular/referrers", headers)
        ])
        paths += metrics.upsert("gh_paths", [
            {"repo": repo, "snapshot_date": today, "path": str(p["path"]), "title": str(p.get("title") or ""),
             "count": int(p["count"]), "uniques": int(p["uniques"])}
            for p in _get_json(http, f"{base}/popular/paths", headers)
        ])
    return f"{len(repos)} repos, {days} repo-days, {refs} referrer rows, {paths} path rows"


def collect_stars(metrics: Metrics, http: Http, owner: str, repos: list[str], token: str) -> str:
    """Stars with timestamps. After the first run, starts from the page the known stars end on."""
    headers = _gh_headers(token, "application/vnd.github.star+json")
    added = 0
    for repo in repos:
        known = metrics.count("gh_stars", repo=repo)
        page = known // 100 + 1
        while page <= 50:
            url = f"{GITHUB_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/stargazers?per_page=100&page={page}"
            batch = _get_json(http, url, headers)
            rows = [
                {"repo": repo, "user_hash": _hash((item.get("user") or {}).get("login") or ""),
                 "starred_at": str(item.get("starred_at") or "")}
                for item in batch if isinstance(item, dict) and item.get("starred_at")
            ]
            before = metrics.count("gh_stars", repo=repo)
            metrics.upsert("gh_stars", rows)
            added += metrics.count("gh_stars", repo=repo) - before
            if len(batch) < 100:
                break
            page += 1
    total = int(metrics.conn.execute("SELECT COUNT(*) FROM gh_stars").fetchone()[0])
    return f"{len(repos)} repos, {total} stars ({added} new)"


def collect_downloads(metrics: Metrics, http: Http, packages: list[str], since: date | None) -> str:
    rows: list[dict] = []
    for package in packages:
        data = _get_json(http, f"{PYPISTATS}/{quote(package.lower(), safe='')}/overall?mirrors=false", {"Accept": "application/json"})
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
                rows.append({
                    "kind": "release", "source": "pypi", "project": package, "version": ref.version,
                    "at": ref.upload_time.replace("Z", "+00:00"),
                    "url": f"https://pypi.org/project/{package}/{ref.version}/", "detail": "",
                })
    broadcasts: list[dict] = []
    for draft in store.list_drafts(status=PUBLISHED):
        broadcasts.append({
            "kind": "broadcast", "source": draft.platform, "project": draft.project, "version": draft.version,
            "at": draft.updated_at, "url": draft.external_id or f"draft:{draft.id}",
            "detail": json.dumps({"draft": draft.id, "kind": draft.kind, "subject": draft.label,
                                  "text": draft.body[:500], "logged": False}),
        })
    for row in store.logged_broadcasts():
        broadcasts.append({
            "kind": "broadcast", "source": row["platform"], "project": row["project"], "version": "",
            "at": row["at"], "url": row["url"],
            "detail": json.dumps({"logged": row["id"], "kind": row["kind"], "subject": row["subject"],
                                  "text": row["text"][:500]}),
        })
    metrics.upsert("events", rows)
    # Broadcasts are rebuilt from their sources, so edits and deletions carry through.
    with metrics.transaction():
        metrics.conn.execute("DELETE FROM events WHERE kind = 'broadcast'")
        metrics.conn.executemany(
            "INSERT OR REPLACE INTO events (kind, source, project, version, at, url, detail) "
            "VALUES (:kind, :source, :project, :version, :at, :url, :detail)",
            broadcasts,
        )
    logged = sum(1 for b in broadcasts if '"logged": false' not in b["detail"])
    return f"{len(rows)} releases, {len(broadcasts)} broadcasts ({logged} logged by hand)"


def _hash(login: str) -> str:
    return hashlib.sha256(login.lower().encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
