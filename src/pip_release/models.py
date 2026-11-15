"""Shared records for releases and the review queue."""

from __future__ import annotations

from dataclasses import dataclass

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
PUBLISHED = "published"
FAILED = "failed"
HANDED_OFF = "handed_off"
PUBLISHING = "publishing"

STATUSES = (PENDING, APPROVED, REJECTED, PUBLISHED, FAILED, HANDED_OFF, PUBLISHING)

# What a draft announces. `release` is one PyPI version; the rest are posts not tied to one.
KINDS = ("release", "digest", "headline", "demo", "note", "video", "page")
POST_KINDS = ("demo", "note", "video", "page")
DRAFT_FIELDS = {"title", "body", "reply", "edited", "note", "external_id", "error"}


@dataclass(frozen=True)
class VersionRef:
    version: str
    yanked: bool
    upload_time: str


@dataclass(frozen=True)
class Release:
    project: str
    version: str
    summary: str
    url: str
    author: str | None
    project_urls: dict[str, str]
    yanked: bool
    notes: str = ""


@dataclass(frozen=True)
class Rendered:
    title: str
    body: str


@dataclass(frozen=True)
class Post:
    """Content that isn't one release: a demo, a build note, a video, a page."""

    kind: str
    subject: str  # short label for the queue, e.g. the video title
    text: str  # what the post says, before the link
    link: str
    title: str = ""  # for platforms with titles (Patreon); defaults to the subject


@dataclass(frozen=True)
class Draft:
    id: int
    project: str
    version: str
    platform: str
    title: str
    body: str
    link: str
    status: str
    edited: bool
    created_at: str
    updated_at: str
    note: str | None
    external_id: str | None
    error: str | None
    kind: str = "release"
    post_key: str = ""
    subject: str = ""
    reply: str = ""  # posted as a reply to the post (the link, when the audience wants it there)
    drafted_by: str = "template"  # "template", or "<backend>:<model>" for assisted drafts

    @property
    def label(self) -> str:
        """How the queue names this draft's content."""
        if self.subject:
            return self.subject
        return f"{self.project} {self.version}".strip()


@dataclass(frozen=True)
class PublishResult:
    external_id: str | None
    detail: str
    handed_off: bool = False
