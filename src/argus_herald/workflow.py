"""Poll PyPI and move drafts through review."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from argus_herald.config import Config, normalize_name
from argus_herald.feeds import FeedItem, FeedSource, read_feed
from argus_herald.http import Http, HttpError, check_url, send_json
from argus_herald.models import (
    APPROVED,
    FAILED,
    HANDED_OFF,
    KINDS,
    PENDING,
    POST_KINDS,
    PUBLISHED,
    PUBLISHING,
    Draft,
    Post,
    PublishResult,
    Release,
)
from argus_herald.notes import fetch_notes
from argus_herald.platforms import Publisher
from argus_herald.privacy import Gate, GateUnavailable, Hit, describe, load_gate
from argus_herald.pypi import fetch_release, list_versions
from argus_herald.store import Store

log = logging.getLogger(__name__)

REMINDER_AFTER = timedelta(hours=24)
REMINDER_KEY = "pending_reminder_at"
BASELINE_KEY = "baselined_at"
STALE_NOTE = "stale backlog"


class ReviewError(Exception):
    pass


class PrivacyRefused(ReviewError):
    def __init__(self, hits: list[Hit]) -> None:
        super().__init__(describe(hits))
        self.hits = hits


def draft_fields(draft: Draft) -> dict[str, str]:
    """The text of a draft the privacy gate reads."""
    return {"title": draft.title, "body": draft.body, "link": draft.link, "reply": draft.reply}


def gate_hits(store: Store, gate: Gate, draft: Draft) -> list[Hit]:
    """Hits in this draft (text and alt text) that haven't been accepted for it."""
    fields = draft_fields(draft)
    for index, item in enumerate(store.media(draft.id), start=1):
        fields[f"alt text {index}"] = item["alt"]
    return gate.check(fields, store.accepted(draft.id))


def media_problems(store: Store, publisher: Publisher, draft: Draft) -> list[str]:
    """Why this draft's attachments can't be approved or published yet."""
    from argus_herald.media import UNSTRIPPED, MediaError, check_limits

    media = store.media(draft.id)
    problems = [f"{m['name']} needs alt text" for m in media if not m["alt"].strip()]
    problems += [
        f"{m['name']}: video metadata wasn't stripped (install ffmpeg and attach it again)"
        for m in media
        if m["removed"] == UNSTRIPPED
    ]
    problems += [f"{m['name']}: the cleaned file is missing" for m in media if not Path(m["path"]).is_file()]
    try:
        check_limits(publisher.media_limits, publisher.label, media)
    except MediaError as exc:
        problems.append(str(exc))
    return problems


def attach_media(
    store: Store,
    media_root: Path,
    draft_id: int,
    source: Path,
    alt: str,
    *,
    all_drafts: bool = False,
    name: str | None = None,
) -> list[int]:
    """Clean a file and attach it to a draft (or every draft of its post). Returns media ids."""
    from argus_herald.media import MediaError, clean

    draft = _require_draft(store, draft_id)
    if draft.status not in (PENDING, APPROVED, FAILED):
        raise ReviewError(f"Draft {draft_id} is {draft.status}; media can't change now")
    try:
        cleaned = clean(source, media_root / "clean")
    except MediaError as exc:
        raise ReviewError(str(exc)) from exc
    targets = [draft]
    if all_drafts:
        targets = [d for d in store.list_drafts(post_key=draft.post_key) if d.status in (PENDING, APPROVED, FAILED)]
    ids = []
    for target in targets:
        ids.append(
            store.add_media(
                target.id,
                kind=cleaned.kind,
                path=str(cleaned.path),
                name=name or source.name,
                alt=alt.strip(),
                size=cleaned.bytes,
                removed=cleaned.removed,
            )
        )
        if target.status == APPROVED:  # new media needs a fresh look
            store.transition(target.id, (APPROVED,), PENDING)
    return ids


def enforce_gate(store: Store, gate: Gate, draft: Draft) -> None:
    hits = gate_hits(store, gate, draft)
    if hits:
        raise PrivacyRefused(hits)


def accept_hit(store: Store, gate: Gate, draft_id: int, key: str, note: str | None) -> None:
    """Let one specific hit through for one draft. The note says why and is required."""
    draft = _require_draft(store, draft_id)
    if not (note or "").strip():
        raise ReviewError("Accepting a privacy hit needs a note saying why it is safe to publish")
    current = {hit.key for hit in gate_hits(store, gate, draft)}
    if key not in current:
        raise ReviewError(f"Draft {draft_id} has no open privacy hit {key!r}")
    store.accept_hit(draft_id, key, note.strip())


@dataclass
class PollEvent:
    kind: str
    project: str
    version: str | None = None
    detail: str = ""
    draft_ids: list[int] = field(default_factory=list)


def tracked_projects(config: Config) -> list[str]:
    ignored = {normalize_name(name) for name in config.ignore}
    return [name for name in config.projects if normalize_name(name) not in ignored]


def poll(
    config: Config,
    store: Store,
    http: Http,
    publishers: list[Publisher],
    *,
    now: datetime | None = None,
) -> list[PollEvent]:
    projects = tracked_projects(config)
    if not projects and not config.feeds and not config.github:
        raise ReviewError(
            "Nothing to watch. Add pypi.projects or sources. PyPI does not publish a per-user release feed."
        )
    events: list[PollEvent] = []
    if not publishers:
        for project in projects:
            if not store.is_baselined(project):
                events.extend(_baseline_only(store, http, project))
        events.extend(poll_feeds(config, store, http, [], draft=False))
        events.extend(poll_github(config, store, http, [], draft=False))
        events.append(
            PollEvent(
                "error",
                (projects or [config.feeds[0].name])[0],
                detail="No destinations are enabled, so new releases were left unseen",
            )
        )
        return events
    for project in projects:
        try:
            versions = list_versions(http, project)
        except (HttpError, OSError, ValueError) as exc:
            events.append(PollEvent("error", project, detail=str(exc)))
            continue
        if not store.is_baselined(project):
            store.baseline(project, [item.version for item in versions])
            latest = versions[-1].version if versions else None
            events.append(
                PollEvent(
                    "baseline",
                    project,
                    latest,
                    detail=_version_count(len(versions)),
                )
            )
            continue
        fresh = [item for item in versions if not store.seen(project, item.version)]
        fresh.sort(key=lambda item: item.upload_time)
        for item in fresh:
            if item.yanked:
                store.mark_seen(project, item.version)
                events.append(PollEvent("skip", project, item.version, detail="yanked"))
                continue
            try:
                if config.digest is not None:
                    events.append(_collect_release(store, http, project, item.version, item.upload_time))
                else:
                    events.append(_draft_release(store, http, publishers, project, item.version))
            except (OSError, ValueError) as exc:
                events.append(PollEvent("error", project, item.version, detail=str(exc)))
    events.extend(poll_github(config, store, http, publishers))
    events.extend(poll_feeds(config, store, http, publishers))
    if config.digest is not None:
        events.extend(close_digests(config, store, publishers, now=now))
    return events


def poll_github(
    config: Config,
    store: Store,
    http: Http,
    publishers: list[Publisher],
    *,
    draft: bool = True,
    env: Mapping[str, str] | None = None,
) -> list[PollEvent]:
    """GitHub Releases. The first read of a repo baselines it; later releases are collected
    (digest mode) or drafted. Prereleases are never drafted on their own."""
    from argus_herald.discover import _github_repos
    from argus_herald.github import list_releases

    source = config.github
    if source is None:
        return []
    token = ((env if env is not None else os.environ).get("GITHUB_TOKEN") or "").strip()
    events: list[PollEvent] = []
    repos = list(source.repos)
    if not repos:
        try:
            repos = [r for r in _github_repos(http, source.owner, token) if r.startswith(source.prefix)]
        except (HttpError, OSError, ValueError) as exc:
            return [PollEvent("error", f"github {source.owner}", detail=str(exc))]
    for repo in repos:
        try:
            releases = list_releases(http, source.owner, repo, token)
        except (HttpError, OSError, ValueError) as exc:
            events.append(PollEvent("error", f"{repo} (github)", detail=str(exc)))
            continue
        if not source.include_prereleases:
            releases = [r for r in releases if not r.prerelease]
        if not store.is_baselined(repo, "github"):
            store.baseline(repo, [r.version for r in releases], "github")
            events.append(
                PollEvent(
                    "baseline",
                    f"{repo} (github)",
                    releases[-1].version if releases else None,
                    detail=_version_count(len(releases)).replace("PyPI", "GitHub"),
                )
            )
            continue
        if not draft:
            continue
        for rel in releases:
            if store.seen(repo, rel.version, "github"):
                continue
            if config.digest is not None:
                added = store.add_release(
                    source="github",
                    project=repo,
                    version=rel.version,
                    released_at=rel.published_at,
                    summary=rel.name,
                    notes=rel.notes,
                    url=rel.url,
                )
                detail = "kept for the next digest" if added else "notes added to the PyPI release"
                events.append(PollEvent("collect", f"{repo} (github)", rel.version, detail=detail))
            elif rel.prerelease:
                events.append(PollEvent("skip", f"{repo} (github)", rel.version, detail="prerelease"))
            else:
                release = Release(
                    project=repo,
                    version=rel.version,
                    summary=rel.name,
                    url=rel.url,
                    author=None,
                    project_urls={},
                    yanked=False,
                    notes=rel.notes,
                )
                ids = _drafts_for(store, publishers, release)
                detail = ", ".join(f"#{i}" for i in ids) or "drafts already existed"
                events.append(
                    PollEvent("draft" if ids else "skip", f"{repo} (github)", rel.version, detail=detail, draft_ids=ids)
                )
            store.mark_seen(repo, rel.version, "github")
    return events


def _drafts_for(store: Store, publishers: list[Publisher], release: Release) -> list[int]:
    """Per-release drafts, for the destinations whose audience wants this package."""
    ids: list[int] = []
    for publisher in publishers:
        if not publisher.wants([release.project]):
            continue
        rendered = publisher.render(release)
        publisher.validate(rendered.title, rendered.body)
        rendered, reply = publisher.shape(rendered, release.url)
        draft_id, created = store.add_draft(
            project=release.project,
            version=release.version,
            platform=publisher.id,
            title=rendered.title,
            body=rendered.body,
            link=release.url,
            reply=reply,
        )
        if created:
            ids.append(draft_id)
    return ids


def _release_with_notes(http: Http, project: str, version: str):
    release = fetch_release(http, project, version)
    if release.yanked:
        return release
    try:
        notes = fetch_notes(http, release.project_urls, release.version)
    except (HttpError, OSError, ValueError):
        log.warning("Release notes for %s %s were skipped", project, version, exc_info=True)
        notes = ""
    return replace(release, notes=notes)


def _collect_release(store: Store, http: Http, project: str, version: str, uploaded: str) -> PollEvent:
    """Digest mode: record the release for its window instead of drafting it."""
    try:
        release = _release_with_notes(http, project, version)
    except (HttpError, OSError, ValueError) as exc:
        return PollEvent("error", project, version, detail=str(exc))
    if release.yanked:
        store.mark_seen(project, version)
        return PollEvent("skip", project, version, detail="yanked")
    released_at = uploaded or datetime.now(UTC).replace(microsecond=0).isoformat()
    store.add_release(
        project=project,
        version=version,
        released_at=released_at.replace("Z", "+00:00"),
        summary=release.summary,
        notes=release.notes,
        url=release.url,
    )
    store.mark_seen(project, version)
    return PollEvent("collect", project, version, detail="kept for the next digest")


def close_digests(
    config: Config, store: Store, publishers: list[Publisher], *, now: datetime | None = None
) -> list[PollEvent]:
    """Create a digest for every window that has closed and has none yet."""
    from argus_herald.digest import build_digest, closed_windows

    settings = config.digest
    if settings is None:
        return []
    now = now or datetime.now(UTC)
    done = {row["period_from"] for row in store.digests()}
    events: list[PollEvent] = []
    for window in closed_windows(settings, now):
        if window.start.isoformat() in done:
            continue
        digest_id = store.create_digest(window.start.isoformat(), window.period_to.isoformat(), window.closes_at)
        try:
            gate: Gate | None = load_gate(config.privacy)
        except GateUnavailable:
            gate = None
        from argus_herald.drafting import configured_backend
        from argus_herald.http import HttpClient

        backend, warning = configured_backend(config.drafting, os.environ, HttpClient())
        ids, status = build_digest(store, settings, publishers, digest_id, gate, backend)
        if warning:
            status = f"{status}; {warning}"
        events.append(
            PollEvent("digest", f"digest {window.start}", str(window.period_to), detail=status, draft_ids=ids)
        )
    return events


# Site categories that may become posts, and the draft kind each one gets.
_SITE_KINDS = {"note": "note", "release": "headline", "digest": "digest"}


def compose(store: Store, publishers: list[Publisher], post: Post, *, post_key: str | None = None) -> list[int]:
    """One pending draft per destination for a post that isn't a release. Returns new ids."""
    if post.kind not in POST_KINDS and post.kind not in _SITE_KINDS.values():
        raise ReviewError(f"Unknown post kind {post.kind!r}. Use one of: {', '.join(POST_KINDS)}")
    if not post.text.strip() and not post.link.strip():
        raise ReviewError("A post needs text, a link, or both")
    if post.link.strip():
        try:
            check_url(post.link.strip())
        except ValueError as exc:
            raise ReviewError(f"Link: {exc}") from exc
    if not publishers:
        raise ReviewError("No destinations are enabled")
    key = post_key or f"post:{uuid4().hex[:12]}"
    ids: list[int] = []
    for publisher in publishers:
        rendered, reply = publisher.shape(publisher.render_post(post), post.link.strip())
        draft_id, created = store.add_draft(
            kind=post.kind,
            post_key=key,
            subject=post.subject or post.title or post.text.strip().split("\n", 1)[0][:80],
            platform=publisher.id,
            title=rendered.title,
            body=rendered.body,
            link=post.link.strip(),
            reply=reply,
        )
        if created:
            ids.append(draft_id)
    return ids


def poll_feeds(
    config: Config, store: Store, http: Http, publishers: list[Publisher], *, draft: bool = True
) -> list[PollEvent]:
    """Read each feed. The first read baselines it; later new items become posts."""
    events: list[PollEvent] = []
    for feed in config.feeds:
        try:
            items = read_feed(http, feed.url)
        except (HttpError, OSError, ValueError) as exc:
            events.append(PollEvent("error", feed.name, detail=str(exc)))
            continue
        if not store.feed_baselined(feed.name):
            store.baseline_feed(feed.name, [item.id for item in items])
            events.append(PollEvent("baseline", feed.name, detail=f"{len(items)} items already in the feed"))
            continue
        if not draft:
            continue
        targets = [p for p in publishers if not feed.platforms or p.id in feed.platforms]
        for item in items:
            if store.feed_seen(feed.name, item.id):
                continue
            post = feed_post(feed, item)
            if post is None:
                store.mark_feed_seen(feed.name, item.id)
                continue
            if not targets:
                events.append(
                    PollEvent("error", feed.name, item.title, detail="no enabled destination for this source")
                )
                continue
            ids = compose(store, targets, post, post_key=f"{feed.name}:{item.id}")
            store.mark_feed_seen(feed.name, item.id)
            labels = ", ".join(f"#{i}" for i in ids) or "drafts already existed"
            events.append(PollEvent("draft", feed.name, item.title, detail=labels, draft_ids=ids))
    return events


def feed_post(feed: FeedSource, item: FeedItem) -> Post | None:
    """The post for a feed item, or None when this source skips it."""
    if feed.name == "site":
        matched = [c for c in item.categories if c in feed.categories]
        if not matched:
            return None
        kind = _SITE_KINDS.get(matched[0], feed.kind)
        text = item.title if not item.summary else f"{item.title}\n\n{item.summary}"
        return Post(kind=kind, subject=item.title, text=text, link=item.link)
    return Post(kind=feed.kind, subject=item.title, text=f"New video: {item.title}", link=item.link)


def _baseline_only(store: Store, http: Http, project: str) -> list[PollEvent]:
    if store.is_baselined(project):
        return []
    try:
        versions = list_versions(http, project)
    except (HttpError, OSError, ValueError) as exc:
        return [PollEvent("error", project, detail=str(exc))]
    store.baseline(project, [item.version for item in versions])
    return [
        PollEvent(
            "baseline",
            project,
            versions[-1].version if versions else None,
            detail=_version_count(len(versions)),
        )
    ]


def _draft_release(
    store: Store,
    http: Http,
    publishers: list[Publisher],
    project: str,
    version: str,
) -> PollEvent:
    try:
        release = fetch_release(http, project, version)
    except (HttpError, OSError, ValueError) as exc:
        return PollEvent("error", project, version, detail=str(exc))
    if release.yanked:
        store.mark_seen(project, version)
        return PollEvent("skip", project, version, detail="yanked")
    try:
        notes = fetch_notes(http, release.project_urls, release.version)
    except (HttpError, OSError, ValueError):
        log.warning("Release notes for %s %s were skipped", project, version, exc_info=True)
        notes = ""
    release = replace(release, notes=notes)
    ids = _drafts_for(store, publishers, release)
    labels = [f"#{draft_id}" for draft_id in ids]
    store.mark_seen(project, version)
    if not ids:
        return PollEvent("skip", project, version, detail="drafts already existed")
    return PollEvent("draft", project, version, detail=", ".join(labels), draft_ids=ids)


def reminder_due(last: str | None, now: datetime, interval: timedelta = REMINDER_AFTER) -> bool:
    """True when pending drafts should be raised again."""

    if not last:
        return True
    try:
        previous = datetime.fromisoformat(last)
    except ValueError:
        return True
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=UTC)
    return now - previous >= interval


def desktop_notice(drafts: list[Draft], review_url: str, *, still_waiting: bool = False) -> tuple[str, str] | None:
    """Summary and body for a desktop notification. None when there is nothing to say."""

    if not drafts:
        return None
    releases: list[str] = []
    for draft in drafts:
        label = draft.label
        if label not in releases:
            releases.append(label)
    count = len(drafts)
    if still_waiting:
        summary = "Draft still waiting" if count == 1 else f"{count} drafts still waiting"
    else:
        summary = "Draft waiting" if count == 1 else f"{count} drafts waiting"
    lines = releases[:6]
    extra = len(releases) - len(lines)
    if extra:
        lines.append(f"and {extra} more")
    if review_url:
        lines.append(review_url)
    return summary, "\n".join(lines)


def notify_review(http: Http, env: Mapping[str, str], drafts: list[Draft], *, event: str = "drafts.created") -> None:
    url = (env.get("ARGUS_HERALD_REVIEW_WEBHOOK") or "").strip()
    if not url or not drafts:
        return
    check_url(url)
    payload = {
        "event": event,
        "drafts": [
            {
                "id": draft.id,
                "kind": draft.kind,
                "subject": draft.label,
                "project": draft.project,
                "version": draft.version,
                "platform": draft.platform,
                "title": draft.title,
                "body": draft.body,
            }
            for draft in drafts
        ],
    }
    review_url = (env.get("ARGUS_HERALD_UI_URL") or "").strip()
    if review_url:
        payload["review_url"] = review_url
    status, _body = send_json(http, url, payload)
    if status not in (200, 201, 202, 204):
        raise HttpError("POST", url, status)


def rebaseline(config: Config, store: Store, http: Http, projects: list[str] | None = None) -> list[PollEvent]:
    """Mark every current version of each project as seen, without drafting.

    Same effect as the first poll, on demand and for projects already baselined.
    Records BASELINE_KEY when every project was read.
    """
    tracked = tracked_projects(config)
    if projects:
        known = {normalize_name(name): name for name in tracked}
        unknown = [name for name in projects if normalize_name(name) not in known]
        if unknown:
            raise ReviewError(f"Not tracked: {', '.join(unknown)}")
        targets = [known[normalize_name(name)] for name in projects]
    else:
        targets = tracked
    if not targets:
        raise ReviewError("No projects to baseline. Add pypi.projects.")
    events: list[PollEvent] = []
    started = datetime.now(UTC).replace(microsecond=0).isoformat()
    for project in targets:
        try:
            versions = list_versions(http, project)
        except (HttpError, OSError, ValueError) as exc:
            events.append(PollEvent("error", project, detail=str(exc)))
            continue
        fresh = sum(1 for item in versions if not store.seen(project, item.version))
        store.baseline(project, [item.version for item in versions])
        events.append(
            PollEvent(
                "baseline",
                project,
                versions[-1].version if versions else None,
                detail=f"{_version_count(len(versions))}, {fresh} newly marked seen",
            )
        )
    if not any(event.kind == "error" for event in events):
        store.set_meta(BASELINE_KEY, started)
    return events


def approve(
    store: Store,
    publisher: Publisher,
    draft_id: int,
    note: str | None,
    *,
    gate: Gate,
    schedule: bool = True,
    schedules: dict | None = None,
) -> Draft:
    """Approve a draft. With a destination schedule, it also gets its next slot (schedule=False:
    leave it for Publish)."""
    draft = _require_draft(store, draft_id)
    publisher.validate(draft.title, draft.body)
    problems = media_problems(store, publisher, draft)
    if problems:
        raise ReviewError("; ".join(problems))
    enforce_gate(store, gate, draft)
    updated = store.transition(draft_id, (PENDING,), APPROVED, note=note, error=None)
    if updated is None:
        raise ReviewError(f"Draft {draft_id} is {draft.status} and cannot be approved")
    if schedule and publisher.schedule is not None:
        from argus_herald.schedule import choose, iso

        when = choose(store, updated, publisher.schedule, datetime.now(UTC), schedules or {})
        if when is not None:
            store.set_publish_at(draft_id, iso(when))
            updated = store.get(draft_id) or updated
    return updated


def reject(store: Store, draft_id: int, note: str | None) -> Draft:
    draft = _require_draft(store, draft_id)
    updated = store.transition(
        draft_id,
        (PENDING, APPROVED, FAILED, HANDED_OFF, PUBLISHING),
        "rejected",
        note=note,
    )
    if updated is None:
        raise ReviewError(f"Draft {draft_id} is {draft.status} and cannot be rejected")
    return updated


def edit_draft(
    store: Store, publisher: Publisher, draft_id: int, title: str, body: str, reply: str | None = None
) -> Draft:
    draft = _require_draft(store, draft_id)
    publisher.validate(title, body)
    if reply:
        publisher.validate("", reply)
    updated = store.update_text(draft_id, title, body, (PENDING, APPROVED, FAILED), reply=reply)
    if updated is None:
        raise ReviewError(f"Draft {draft_id} is {draft.status} and cannot be edited")
    return updated


def publish_draft(
    store: Store,
    publisher: Publisher,
    draft: Draft,
    env: Mapping[str, str],
    http: Http,
    *,
    gate: Gate,
    dry_run: bool = False,
) -> PublishResult:
    if draft.status not in (APPROVED, FAILED, PUBLISHING):
        raise ReviewError(f"Draft {draft.id} is {draft.status}. Approve it before publishing.")
    # Checked again here: the deny-list or config may have changed since approval.
    enforce_gate(store, gate, draft)
    problems = media_problems(store, publisher, draft)
    if problems:
        raise ReviewError("; ".join(problems))
    if dry_run:
        return PublishResult(external_id=None, detail=draft.body, handed_off=publisher.manual)
    locked = store.transition(draft.id, (APPROVED, FAILED, PUBLISHING), PUBLISHING, error=None)
    if locked is None:
        raise ReviewError(f"Draft {draft.id} changed before it could be published")
    try:
        result = publisher.publish(locked, env, http)
    except Exception as exc:
        store.transition(draft.id, (PUBLISHING,), FAILED, error=str(exc)[:500])
        raise
    status = HANDED_OFF if result.handed_off else PUBLISHED
    store.transition(
        draft.id,
        (PUBLISHING,),
        status,
        external_id=result.external_id,
        error=None,
        note=result.detail if result.handed_off else locked.note,
    )
    return result


def confirm(store: Store, draft_id: int, url: str | None = None) -> Draft:
    """Record that a person posted a handed-off draft, with the post URL when known."""
    draft = _require_draft(store, draft_id)
    fields: dict[str, object] = {}
    if url and url.strip():
        fields["external_id"] = post_url(draft.platform, url)
    # A published draft without a URL can still get one afterwards.
    allowed = (HANDED_OFF, PUBLISHED) if fields and not draft.external_id else (HANDED_OFF,)
    updated = store.transition(draft_id, allowed, PUBLISHED, **fields)
    if updated is None:
        raise ReviewError(f"Draft {draft_id} is {draft.status}. Confirm is for a manual handoff (X, Patreon).")
    return updated


_X_HOSTS = {"x.com", "twitter.com", "www.x.com", "www.twitter.com", "mobile.x.com", "mobile.twitter.com"}
_X_STATUS = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/(\d{1,25})/?$")


def post_url(platform: str, raw: str) -> str:
    """Check a pasted post URL and return it in canonical form (X posts as https://x.com/<user>/status/<id>)."""
    url = raw.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ReviewError("The post URL must be a plain https:// link")
    if platform == "twitter":
        match = _X_STATUS.match(parsed.path)
        if parsed.hostname.lower() not in _X_HOSTS or not match:
            raise ReviewError("An X post URL looks like https://x.com/<user>/status/<id>")
        return f"https://x.com/{match.group(1)}/status/{match.group(2)}"
    return url


def _version_count(count: int) -> str:
    noun = "version" if count == 1 else "versions"
    return f"{count} {noun} already on PyPI"


def _require_draft(store: Store, draft_id: int) -> Draft:
    draft = store.get(draft_id)
    if draft is None:
        raise ReviewError(f"No draft {draft_id}")
    return draft


# ---- broadcasts made outside herald ----------------------------------------------------

_PLATFORM = re.compile(r"^[a-z0-9][a-z0-9.-]{0,39}$")
LOGGED_KINDS = (*KINDS, "other")
# Free-text platform names that mean X, so their URLs are checked and normalised like X posts.
_X_NAMES = {"x", "twitter"}


def logged_fields(
    *,
    platform: str,
    url: str,
    kind: str = "other",
    project: str = "",
    subject: str = "",
    text: str = "",
    at: str | None = None,
) -> dict[str, str]:
    """Validate and normalise one logged broadcast."""
    platform = platform.strip().lower()
    if not _PLATFORM.match(platform):
        raise ReviewError("Platform is a short name like reddit, hn, youtube or newsletter")
    if kind not in LOGGED_KINDS:
        raise ReviewError(f"Kind is one of: {', '.join(LOGGED_KINDS)}")
    canonical = post_url("twitter" if platform in _X_NAMES else platform, url)
    return {
        "platform": "twitter" if platform in _X_NAMES else platform,
        "url": canonical,
        "kind": kind,
        "project": project.strip(),
        "subject": subject.strip()[:200],
        "text": text.strip()[:5000],
        "at": _utc(at) if at else datetime.now(UTC).replace(microsecond=0).isoformat(),
    }


def _utc(value: str) -> str:
    try:
        if len(value.strip()) == 10:
            stamp = datetime.fromisoformat(value.strip() + "T12:00:00")
        else:
            stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewError(f"--at must be an ISO date or timestamp, not {value!r}") from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).replace(microsecond=0).isoformat()


def log_broadcast(store: Store, **fields: str) -> int:
    try:
        return store.log_broadcast(**logged_fields(**fields))
    except ValueError as exc:
        raise ReviewError(str(exc)) from exc


def edit_logged(store: Store, broadcast_id: int, **changes: str) -> None:
    row = store.logged_broadcast(broadcast_id)
    if row is None:
        raise ReviewError(f"No logged broadcast {broadcast_id}")
    merged = {k: row[k] for k in ("platform", "url", "kind", "project", "subject", "text", "at")}
    merged.update({k: v for k, v in changes.items() if v is not None})
    try:
        store.update_logged(broadcast_id, **logged_fields(**merged))
    except ValueError as exc:
        raise ReviewError(str(exc)) from exc
