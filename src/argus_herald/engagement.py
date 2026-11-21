"""Engagement per post, and site traffic. Rows go to metrics.sqlite (see metrics.py for the schema).

- Bluesky and Mastodon: public read APIs, no login. Each published post is sampled at +1h,
  +24h, +72h and +7d after it went out, so the dashboard can draw an engagement curve. After
  the 7-day sample a post isn't fetched again.
- X: no free API. `argus-herald import x-analytics <file.csv>` reads the analytics export: per-post
  rows are matched to broadcasts by post id, daily account rows go to account_engagement. A
  week's totals can be entered by hand with `import x-week`.
- Site traffic: Cloudflare Web Analytics through the GraphQL Analytics API (CF_API_TOKEN with
  Account Analytics: read, CF_ACCOUNT_ID, CF_SITE_TAG), daily per path and per referrer.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote, urlparse

from argus_herald.http import Http, HttpError, send_json

CHECKPOINTS = (timedelta(hours=1), timedelta(hours=24), timedelta(hours=72), timedelta(days=7))
BSKY = "https://public.api.bsky.app/xrpc"
CF_GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
X_PLATFORMS = ("twitter", "x")
BSKY_PLATFORMS = ("bluesky", "bsky")
MASTODON_PLATFORMS = ("mastodon",)


def _parse(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


# ---- which posts are due a sample --------------------------------------------------------


@dataclass(frozen=True)
class Post:
    platform: str
    url: str
    at: datetime


def due_posts(conn, platforms: Iterable[str], now: datetime) -> list[Post]:
    """Broadcasts whose latest passed checkpoint has no sample at or after it yet."""
    platforms = tuple(platforms)
    marks = ", ".join("?" for _ in platforms)
    out = []
    for row in conn.execute(
        f"SELECT source, url, at FROM events WHERE kind = 'broadcast' AND source IN ({marks})", platforms
    ):
        if str(row["url"]).startswith("draft:"):
            continue
        try:
            at = _parse(row["at"])
        except ValueError:
            continue
        passed = [c for c in CHECKPOINTS if now - at >= c]
        if not passed:
            continue
        since = _iso(at + passed[-1])
        sampled = conn.execute(
            "SELECT 1 FROM post_metrics WHERE platform = ? AND external_id = ? AND fetched_at >= ? LIMIT 1",
            (row["source"], row["url"], since),
        ).fetchone()
        if not sampled:
            out.append(Post(row["source"], row["url"], at))
    return out


# ---- Bluesky -----------------------------------------------------------------------------

_BSKY_WEB = re.compile(r"^/profile/([^/]+)/post/([A-Za-z0-9]+)/?$")
_AT_URI = re.compile(r"^at://([^/]+)/app\.bsky\.feed\.post/([A-Za-z0-9]+)$")


def bluesky_ref(url: str) -> tuple[str, str] | None:
    """(handle or DID, record key) from an at:// URI or a bsky.app post link."""
    match = _AT_URI.match(url)
    if match:
        return match.group(1), match.group(2)
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname in ("bsky.app", "www.bsky.app"):
        match = _BSKY_WEB.match(parsed.path)
        if match:
            return match.group(1), match.group(2)
    return None


def bluesky_samples(http: Http, posts: list[Post], now: datetime) -> tuple[list[dict], list[str]]:
    rows, problems = [], []
    dids: dict[str, str] = {}
    wanted: dict[str, Post] = {}
    for post in posts:
        ref = bluesky_ref(post.url)
        if ref is None:
            problems.append(f"bluesky: can't read a post id from {post.url}")
            continue
        actor, rkey = ref
        if not actor.startswith("did:"):
            if actor not in dids:
                # A renamed or deleted handle costs its own posts only, and is asked about once.
                try:
                    data = _get(http, f"{BSKY}/com.atproto.identity.resolveHandle?handle={quote(actor, safe='')}")
                    did = data.get("did") if isinstance(data, dict) else None
                except (HttpError, OSError, ValueError):
                    did = None
                dids[actor] = str(did) if did else ""
            if not dids[actor]:
                problems.append(f"bluesky: can't resolve handle {actor} for {post.url}")
                continue
            actor = dids[actor]
        wanted[f"at://{actor}/app.bsky.feed.post/{rkey}"] = post
    uris = list(wanted)
    for start in range(0, len(uris), 25):
        batch = uris[start : start + 25]
        query = "&".join(f"uris={quote(u, safe='')}" for u in batch)
        data = _get(http, f"{BSKY}/app.bsky.feed.getPosts?{query}")
        found = {str(p.get("uri")): p for p in data.get("posts") or []}
        for uri in batch:
            post = wanted[uri]
            item = found.get(uri)
            if item is None:
                problems.append(f"bluesky: {post.url} not found (deleted?)")
                continue
            rows.append(
                _row(
                    post,
                    now,
                    likes=item.get("likeCount"),
                    reposts=item.get("repostCount"),
                    replies=item.get("replyCount"),
                    quotes=item.get("quoteCount"),
                )
            )
    return rows, problems


# ---- Mastodon ----------------------------------------------------------------------------

_MASTODON_ID = re.compile(r"^/(?:@[^/]+|users/[^/]+/statuses)/(\d+)/?$")


def mastodon_ref(url: str) -> tuple[str, str] | None:
    """(https://instance, status id) from a status link."""
    parsed = urlparse(url)
    try:
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        parsed.port  # noqa: B018 - raises ValueError for a port that isn't a number
    except ValueError:
        return None
    host = parsed.netloc.rsplit("@", 1)[-1]  # keeps the port and an IPv6 literal's brackets, drops userinfo
    match = _MASTODON_ID.match(parsed.path)
    return (f"https://{host}", match.group(1)) if match else None


def mastodon_samples(http: Http, posts: list[Post], now: datetime) -> tuple[list[dict], list[str]]:
    rows, problems = [], []
    for post in posts:
        ref = mastodon_ref(post.url)
        if ref is None:
            problems.append(f"mastodon: can't read a status id from {post.url}")
            continue
        base, status_id = ref
        try:
            item = _get(http, f"{base}/api/v1/statuses/{status_id}")
        except HttpError as exc:
            if exc.status in (404, 410):
                problems.append(f"mastodon: {post.url} not found (deleted?)")
                continue
            raise
        rows.append(
            _row(
                post,
                now,
                likes=item.get("favourites_count"),
                reposts=item.get("reblogs_count"),
                replies=item.get("replies_count"),
                quotes=item.get("quotes_count"),
            )
        )
    return rows, problems


def _row(post: Post, now: datetime, **counts) -> dict:
    row = {
        "platform": post.platform,
        "external_id": post.url,
        "fetched_at": _iso(now),
        "likes": None,
        "reposts": None,
        "replies": None,
        "quotes": None,
        "impressions": None,
        "engagements": None,
        "link_clicks": None,
        "profile_visits": None,
        "source": "api",
    }
    row.update({k: (int(v) if v is not None else None) for k, v in counts.items()})
    return row


@dataclass
class PostReport:
    lines: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def collect_posts(metrics, http: Http, now: datetime | None = None) -> PostReport:
    """Sample every Bluesky and Mastodon post that has reached a checkpoint. Cheap when none have."""
    now = now or datetime.now(UTC)
    report = PostReport()
    for name, platforms, fetch in (
        ("bluesky", BSKY_PLATFORMS, bluesky_samples),
        ("mastodon", MASTODON_PLATFORMS, mastodon_samples),
    ):
        posts = due_posts(metrics.conn, platforms, now)
        if not posts:
            continue
        try:
            rows, problems = fetch(http, posts, now)
        except HttpError as exc:
            report.errors.append(f"post_metrics {name}: {exc}")
            continue
        except (OSError, ValueError, KeyError) as exc:
            report.errors.append(f"post_metrics {name}: {exc}")
            continue
        metrics.upsert("post_metrics", rows)
        report.skipped.extend(problems)
        report.lines.append(f"post_metrics {name}: {len(rows)} of {len(posts)} due posts sampled")
    return report


# ---- X analytics import ------------------------------------------------------------------

_ALIASES = {
    "post_id": ("postid", "tweetid", "id"),
    "link": ("postlink", "tweetpermalink", "permalink", "link", "url"),
    "date": ("date", "time", "createdat", "postdate"),
    "impressions": ("impressions",),
    "engagements": ("engagements",),
    "likes": ("likes",),
    "reposts": ("reposts", "retweets"),
    "replies": ("replies",),
    "quotes": ("quotes", "quoteposts"),
    "link_clicks": ("urlclicks", "linkclicks"),
    "profile_visits": ("profilevisits", "userprofileclicks"),
    "new_follows": ("newfollows", "follows"),
}
_COUNTS = (
    "impressions",
    "engagements",
    "likes",
    "reposts",
    "replies",
    "quotes",
    "link_clicks",
    "profile_visits",
    "new_follows",
)
_X_STATUS = re.compile(r"/status(?:es)?/(\d{1,25})")
_DATE_FORMATS = ("%a, %b %d, %Y", "%b %d, %Y", "%Y-%m-%d %H:%M %z", "%Y-%m-%d %H:%M:%S %z", "%m/%d/%Y", "%d/%m/%Y")


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


def _columns(headers: list[str]) -> dict[str, str]:
    by_key = {_key(h): h for h in headers}
    out = {}
    for name, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in by_key:
                out[name] = by_key[alias]
                break
    return out


def _number(value: str | None) -> int | None:
    text = (value or "").strip().replace(",", "")
    if not text or text in ("-", "—"):
        return None
    try:
        try:
            return int(text)  # exact, however large: a float loses whole numbers past 2**53
        except ValueError:
            return int(float(text))  # "12.0", "1e3"; int() of inf raises OverflowError, of nan ValueError
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"not a number: {value!r}") from exc


def _date(value: str) -> str:
    text = value.strip()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"can't read the date {value!r}")


@dataclass
class XImport:
    posts: int = 0
    matched: int = 0
    days: int = 0
    skipped: list[str] = field(default_factory=list)


def import_x_analytics(metrics, text: str, account: str, now: datetime | None = None) -> XImport:
    """Per-post rows (with a post id or link) or daily account rows (date, no post id)."""
    now = now or datetime.now(UTC)
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    cols = _columns(list(reader.fieldnames or []))
    if not ({"impressions", "engagements", "likes"} & set(cols)):
        raise ValueError("this doesn't look like an X analytics export (no impressions, engagements or likes column)")
    per_post = "post_id" in cols or "link" in cols
    if not per_post and "date" not in cols:
        raise ValueError("the export has neither a post id nor a date column")
    # Broadcasts by X status id, so a post is stored under the same URL the dashboard knows it by.
    known: dict[str, tuple[str, str]] = {}
    for row in metrics.conn.execute(
        f"SELECT source, url FROM events WHERE kind = 'broadcast' AND source IN ({', '.join('?' for _ in X_PLATFORMS)})",
        X_PLATFORMS,
    ):
        match = _X_STATUS.search(str(row["url"]))
        if match:
            known[match.group(1)] = (row["source"], row["url"])
    result = XImport()
    post_rows, day_rows = [], []
    for line, raw in enumerate(reader, start=2):
        get = lambda name, raw=raw: raw.get(cols[name]) if name in cols else None  # noqa: E731
        try:
            counts = {name: _number(get(name)) for name in _COUNTS}
            if per_post:
                post_id = (get("post_id") or "").strip()
                if not post_id.isdigit():
                    match = _X_STATUS.search(get("link") or "")
                    post_id = match.group(1) if match else ""
                if not post_id:
                    result.skipped.append(f"line {line}: no post id")
                    continue
                platform, url = known.get(post_id, ("twitter", f"https://x.com/{account}/status/{post_id}"))
                result.matched += post_id in known
                counts.pop("new_follows")
                post_rows.append(
                    {
                        "platform": platform,
                        "external_id": url,
                        "fetched_at": _iso(now),
                        **counts,
                        "source": "x-analytics",
                    }
                )
                result.posts += 1
            else:
                day = _date(get("date") or "")
                day_rows.append(
                    {
                        "platform": "twitter",
                        "account": account,
                        "start_date": day,
                        "end_date": day,
                        **{k: v for k, v in counts.items() if k != "quotes"},
                        "source": "x-analytics",
                    }
                )
                result.days += 1
        except ValueError as exc:
            result.skipped.append(f"line {line}: {exc}")
    metrics.upsert("post_metrics", post_rows)
    metrics.upsert("account_engagement", day_rows)
    return result


def record_x_week(metrics, account: str, start: date, end: date, **counts: int | None) -> None:
    """A period's totals entered by hand, for when only the overview numbers are at hand."""
    row = {"platform": "twitter", "account": account, "start_date": start.isoformat(), "end_date": end.isoformat()}
    row.update({k: counts.get(k) for k in _COUNTS if k != "quotes"})
    metrics.upsert("account_engagement", [{**row, "source": "manual"}])


# ---- site traffic (Cloudflare Web Analytics) ---------------------------------------------

CF_QUERY = """
query ($account: String!, $filter: AccountRumPageloadEventsAdaptiveGroupsFilter_InputObject) {
  viewer {
    accounts(filter: {accountTag: $account}) {
      paths: rumPageloadEventsAdaptiveGroups(limit: 5000, filter: $filter) {
        count
        sum { visits }
        dimensions { date requestHost requestPath }
      }
      referrers: rumPageloadEventsAdaptiveGroups(limit: 5000, filter: $filter) {
        count
        sum { visits }
        dimensions { date refererHost }
      }
    }
  }
}
"""


def collect_site(metrics, http: Http, env: Mapping[str, str], today: date | None = None, days: int = 7) -> str:
    """The last `days` days of page views per host and path, and per referrer. Re-fetching a day replaces it."""
    missing = site_configured(env)
    if missing:
        # A blank site tag can answer with no rows, and no rows would replace the stored days.
        raise ValueError(f"Cloudflare site traffic isn't configured: set {', '.join(missing)}")
    token, account, site = (env.get(k, "").strip() for k in ("CF_API_TOKEN", "CF_ACCOUNT_ID", "CF_SITE_TAG"))
    today = today or datetime.now(UTC).date()
    start = today - timedelta(days=days - 1)
    variables = {
        "account": account,
        "filter": {
            "AND": [
                {"date_geq": start.isoformat()},
                {"date_leq": today.isoformat()},
                {"siteTag": site},
                {"bot": 0},
            ]
        },
    }
    status, body = send_json(
        http, CF_GRAPHQL, {"query": CF_QUERY, "variables": variables}, {"Authorization": f"Bearer {token}"}
    )
    if status != 200:
        raise HttpError("POST", CF_GRAPHQL, status)
    data = json.loads(body.decode("utf-8"))
    if data.get("errors"):
        raise ValueError("Cloudflare: " + "; ".join(str(e.get("message", e)) for e in data["errors"])[:300])
    accounts = ((data.get("data") or {}).get("viewer") or {}).get("accounts") or []
    if not accounts:
        raise ValueError("Cloudflare returned no account (check CF_ACCOUNT_ID and the token's scope)")
    paths, refs = [], []
    for item in accounts[0].get("paths") or []:
        dims = item.get("dimensions") or {}
        paths.append(
            {
                "date": str(dims["date"]),
                "host": str(dims.get("requestHost") or "").lower(),
                "path": str(dims.get("requestPath") or "/"),
                "pageviews": int(item.get("count") or 0),
                "visits": int((item.get("sum") or {}).get("visits") or 0),
            }
        )
    for item in accounts[0].get("referrers") or []:
        dims = item.get("dimensions") or {}
        refs.append(
            {
                "date": str(dims["date"]),
                "referrer": str(dims.get("refererHost") or "") or "(direct)",
                "pageviews": int(item.get("count") or 0),
                "visits": int((item.get("sum") or {}).get("visits") or 0),
            }
        )
    with metrics.transaction():
        for table, rows, keys in (
            ("site_traffic", paths, ("host", "path")),
            ("site_referrers", refs, ("referrer",)),
        ):
            # Only the fetched window: a backfill of earlier days must not erase later ones.
            metrics.conn.execute(
                f"DELETE FROM {table} WHERE date BETWEEN ? AND ?", (start.isoformat(), today.isoformat())
            )
            columns = ("date", *keys, "pageviews", "visits")
            metrics.conn.executemany(
                f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [tuple(r[c] for c in columns) for r in rows],
            )
    views = sum(p["pageviews"] for p in paths)
    hosts = len({p["host"] for p in paths if p["host"]})
    pages = len({(p["host"], p["path"]) for p in paths})
    return f"{days} days, {hosts} hosts, {pages} paths, {views} page views, {len(refs)} referrer rows"


def site_configured(env: Mapping[str, str]) -> list[str]:
    """Names of the missing Cloudflare settings (empty when site traffic can be collected)."""
    return [k for k in ("CF_API_TOKEN", "CF_ACCOUNT_ID", "CF_SITE_TAG") if not env.get(k, "").strip()]


def _get(http: Http, url: str) -> dict:
    status, body = http.request("GET", url, headers={"Accept": "application/json"})
    if status != 200:
        raise HttpError("GET", url, status)
    return json.loads(body.decode("utf-8"))
