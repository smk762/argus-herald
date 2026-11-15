"""Atom feeds that become posts: build notes from the site, and new videos from YouTube."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import quote

from pip_release.http import Http, HttpError, check_url

_ATOM = "{http://www.w3.org/2005/Atom}"
_MEDIA = "{http://search.yahoo.com/mrss/}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
MAX_FEED_BYTES = 2_000_000


@dataclass(frozen=True)
class FeedSource:
    """One configured feed. `name` is also the prefix of its post keys ("site", "youtube")."""

    name: str
    url: str
    kind: str  # post kind for items without a usable category: "note", "video", ...
    categories: tuple[str, ...] = ()  # site: only entries with one of these categories
    platforms: tuple[str, ...] = ()  # empty: every enabled destination


@dataclass(frozen=True)
class FeedItem:
    id: str
    title: str
    link: str
    published: str
    summary: str = ""
    categories: tuple[str, ...] = field(default_factory=tuple)
    content: str = ""  # <content>, as given (GitHub release feeds put the notes here as HTML)


def youtube_feed_url(channel_id: str) -> str:
    if not CHANNEL_ID.match(channel_id):
        raise ValueError(f"Not a YouTube channel id: {channel_id!r} (it starts with UC and has 24 characters)")
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={quote(channel_id)}"


def read_feed(http: Http, url: str) -> list[FeedItem]:
    check_url(url)
    status, body = http.request("GET", url, headers={"Accept": "application/atom+xml, application/xml"})
    if status != 200:
        raise HttpError("GET", url, status)
    return parse_atom(body)


def parse_atom(body: bytes) -> list[FeedItem]:
    """Entries of an Atom feed, oldest first. Refuses DTDs, so no entity expansion is possible."""
    if len(body) > MAX_FEED_BYTES:
        raise ValueError("Feed is too large")
    head = body[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in body.lower():
        raise ValueError("Feed declares a DTD or entities; refusing to parse it")
    root = ET.fromstring(body)
    if root.tag != f"{_ATOM}feed":
        raise ValueError("Not an Atom feed")
    items: list[FeedItem] = []
    for entry in root.findall(f"{_ATOM}entry"):
        entry_id = _text(entry, f"{_ATOM}id")
        title = _text(entry, f"{_ATOM}title")
        link = ""
        for node in entry.findall(f"{_ATOM}link"):
            if node.get("rel", "alternate") == "alternate" and node.get("href"):
                link = node.get("href", "")
                break
        if not entry_id or not link:
            continue
        summary = _text(entry, f"{_ATOM}summary") or _text(entry, f"{_MEDIA}group/{_MEDIA}description")
        categories = tuple(
            node.get("term", "").strip().lower()
            for node in entry.findall(f"{_ATOM}category")
            if node.get("term")
        )
        items.append(
            FeedItem(
                id=entry_id,
                title=" ".join(title.split()),
                link=link,
                published=_text(entry, f"{_ATOM}published") or _text(entry, f"{_ATOM}updated"),
                summary=summary.strip(),
                categories=categories,
                content=_text(entry, f"{_ATOM}content"),
            )
        )
    items.sort(key=lambda item: item.published)
    return items


def _text(node: ET.Element, path: str) -> str:
    found = node.find(path)
    return (found.text or "").strip() if found is not None else ""
