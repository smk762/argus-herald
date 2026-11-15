"""Poll PyPI and move drafts through review."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from pip_release.config import Config, normalize_name
from pip_release.http import Http, HttpError, check_url, send_json
from pip_release.models import (
    APPROVED,
    FAILED,
    HANDED_OFF,
    PENDING,
    PUBLISHED,
    PUBLISHING,
    Draft,
    PublishResult,
)
from pip_release.notes import fetch_notes
from pip_release.platforms import Publisher
from pip_release.privacy import Gate, Hit, describe
from pip_release.pypi import fetch_release, list_versions
from pip_release.store import Store

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
    return {"title": draft.title, "body": draft.body, "link": draft.link}


def gate_hits(store: Store, gate: Gate, draft: Draft) -> list[Hit]:
    """Hits in this draft that haven't been accepted for it."""
    return gate.check(draft_fields(draft), store.accepted(draft.id))


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
) -> list[PollEvent]:
    projects = tracked_projects(config)
    if not projects:
        raise ReviewError(
            "No projects to watch. Add pypi.projects. "
            "PyPI does not publish a per-user release feed."
        )
    events: list[PollEvent] = []
    if not publishers:
        for project in projects:
            if not store.is_baselined(project):
                events.extend(_baseline_only(store, http, project))
        events.append(
            PollEvent(
                "error",
                projects[0],
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
                events.append(_draft_release(store, http, publishers, project, item.version))
            except (OSError, ValueError) as exc:
                events.append(PollEvent("error", project, item.version, detail=str(exc)))
    return events


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
    ids: list[int] = []
    labels: list[str] = []
    for publisher in publishers:
        rendered = publisher.render(release)
        publisher.validate(rendered.title, rendered.body)
        draft_id, created = store.add_draft(
            project=project,
            version=version,
            platform=publisher.id,
            title=rendered.title,
            body=rendered.body,
            link=release.url,
        )
        if created:
            ids.append(draft_id)
            labels.append(f"{publisher.id} #{draft_id}")
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
        previous = previous.replace(tzinfo=timezone.utc)
    return now - previous >= interval


def desktop_notice(
    drafts: list[Draft], review_url: str, *, still_waiting: bool = False
) -> tuple[str, str] | None:
    """Summary and body for a desktop notification. None when there is nothing to say."""

    if not drafts:
        return None
    releases: list[str] = []
    for draft in drafts:
        label = f"{draft.project} {draft.version}"
        if label not in releases:
            releases.append(label)
    count = len(drafts)
    if still_waiting:
        summary = "PyPI draft still waiting" if count == 1 else f"{count} PyPI drafts still waiting"
    else:
        summary = "PyPI draft waiting" if count == 1 else f"{count} PyPI drafts waiting"
    lines = releases[:6]
    extra = len(releases) - len(lines)
    if extra:
        lines.append(f"and {extra} more")
    if review_url:
        lines.append(review_url)
    return summary, "\n".join(lines)


def notify_review(
    http: Http, env: Mapping[str, str], drafts: list[Draft], *, event: str = "drafts.created"
) -> None:
    url = (env.get("PIP_RELEASE_REVIEW_WEBHOOK") or "").strip()
    if not url or not drafts:
        return
    check_url(url)
    payload = {
        "event": event,
        "drafts": [
            {
                "id": draft.id,
                "project": draft.project,
                "version": draft.version,
                "platform": draft.platform,
                "title": draft.title,
                "body": draft.body,
            }
            for draft in drafts
        ],
    }
    review_url = (env.get("PIP_RELEASE_UI_URL") or "").strip()
    if review_url:
        payload["review_url"] = review_url
    status, _body = send_json(http, url, payload)
    if status not in (200, 201, 202, 204):
        raise HttpError("POST", url, status)


def rebaseline(
    config: Config, store: Store, http: Http, projects: list[str] | None = None
) -> list[PollEvent]:
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
    started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
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
    store: Store, publisher: Publisher, draft_id: int, note: str | None, *, gate: Gate
) -> Draft:
    draft = _require_draft(store, draft_id)
    publisher.validate(draft.title, draft.body)
    enforce_gate(store, gate, draft)
    updated = store.transition(
        draft_id, (PENDING,), APPROVED, note=note, error=None
    )
    if updated is None:
        raise ReviewError(f"Draft {draft_id} is {draft.status} and cannot be approved")
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
    store: Store, publisher: Publisher, draft_id: int, title: str, body: str
) -> Draft:
    draft = _require_draft(store, draft_id)
    publisher.validate(title, body)
    updated = store.update_text(draft_id, title, body, (PENDING, APPROVED, FAILED))
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
        raise ReviewError(
            f"Draft {draft.id} is {draft.status}. Approve it before publishing."
        )
    # Checked again here: the deny-list or config may have changed since approval.
    enforce_gate(store, gate, draft)
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
        raise ReviewError(
            f"Draft {draft_id} is {draft.status}. Confirm is for a manual handoff (X, Patreon)."
        )
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
