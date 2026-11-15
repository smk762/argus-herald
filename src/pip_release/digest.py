"""Digest mode: collect releases into fixed windows and announce each window once.

When a window closes, herald creates one digest holding every release seen before the close
(releases seen late roll into the next window), then drafts:

- a site page: Markdown with front matter for the site's `src/content/updates/`, written with
  `draft: true` to a local checkout. Herald never commits or pushes. It writes a page only when
  no file is there, or when the file is still exactly what herald wrote last time, and only
  after the privacy gate passes;
- one social draft per destination: a headline, up to three notable changes, and the link to
  the site page.

A release can also be promoted to its own post (kind `headline`). Herald suggests promotion,
never posts it: a major or minor bump (or a first release) whose notes have an Added or feature
section. Patch releases and prereleases are never suggested.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pip_release.config import DigestSettings
from pip_release.models import Post, Rendered, Release
from pip_release.platforms import Publisher
from pip_release.privacy import Gate, describe
from pip_release.store import Store

MAX_HIGHLIGHTS = 3


@dataclass(frozen=True)
class Window:
    start: date  # first day covered
    end: date  # the day the window closes (not covered)

    @property
    def period_to(self) -> date:
        return self.end - timedelta(days=1)

    @property
    def closes_at(self) -> str:
        return f"{self.end.isoformat()}T00:00:00+00:00"

    @property
    def slug(self) -> str:
        return f"{self.end.isoformat()}-digest"


def window_at(settings: DigestSettings, index: int) -> Window:
    start = settings.anchor + timedelta(days=index * settings.window_days)
    return Window(start, start + timedelta(days=settings.window_days))


def current_window(settings: DigestSettings, now: datetime) -> Window:
    days = (now.astimezone(timezone.utc).date() - settings.anchor).days
    return window_at(settings, max(days, 0) // settings.window_days)


def closed_windows(settings: DigestSettings, now: datetime) -> list[Window]:
    """Every window that has closed by `now`, oldest first."""
    today = now.astimezone(timezone.utc).date()
    out: list[Window] = []
    index = 0
    while (window := window_at(settings, index)).end <= today:
        out.append(window)
        index += 1
    return out


# ---- versions and highlights -------------------------------------------------------------

_VERSION = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?")
_PRE = re.compile(r"(?i)(a|alpha|b|beta|rc|c|pre|preview|dev)\d*")


def parse_version(version: str) -> tuple[int, int, int] | None:
    match = _VERSION.match(version.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def is_prerelease(version: str) -> bool:
    match = _VERSION.match(version.strip())
    rest = version.strip()[match.end():] if match else version
    return bool(_PRE.search(rest))


_ADDED_HEADING = re.compile(r"(?im)^\s{0,3}#{1,6}\s*(added|features?|new)\b")
_ADDED_BULLET = re.compile(r"(?im)^\s*[-*]\s+(add(s|ed)?|new)\b")


def has_added(notes: str) -> bool:
    return bool(_ADDED_HEADING.search(notes) or _ADDED_BULLET.search(notes))


def suggest_headline(version: str, earlier: list[str], notes: str) -> bool:
    """A major/minor bump, or a first release, whose notes add something. Never a patch."""
    current = parse_version(version)
    if current is None or is_prerelease(version) or not has_added(notes):
        return False
    previous = [
        parsed for v in earlier
        if not is_prerelease(v) and (parsed := parse_version(v)) is not None and parsed < current
    ]
    if not previous:
        return True
    return current[:2] > max(previous)[:2]


_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def _bullets(notes: str) -> tuple[list[str], list[str]]:
    """(all bullets, bullets under an Added/feature heading), wrapped lines joined."""
    bullets: list[str] = []
    added: list[str] = []
    in_added = False
    current: list[str] | None = None
    for line in notes.splitlines():
        if re.match(r"^\s{0,3}#{1,6}\s", line):
            in_added = bool(_ADDED_HEADING.match(line))
            current = None
            continue
        bullet = re.match(r"^\s*[-*]\s+(.*\S)", line)
        if bullet:
            current = [bullet.group(1)]
            bullets.append(current)  # type: ignore[arg-type]
            if in_added:
                added.append(current)  # type: ignore[arg-type]
        elif current is not None and line.strip() and line[:1].isspace():
            current.append(line.strip())
        elif not line.strip():
            current = None
    join = lambda parts: " ".join(parts)  # noqa: E731
    return [join(b) for b in bullets], [join(b) for b in added]  # type: ignore[arg-type]


def highlight(notes: str, summary: str = "", limit: int = 120) -> str:
    """One line for a release: the first sentence of the first Added bullet, else of the first
    bullet, else the summary."""
    bullets, added = _bullets(notes)
    chosen = (added or bullets or [summary])[0] if (added or bullets or summary) else ""
    chosen = " ".join(_LINK.sub(r"\1", chosen).replace("**", "").split())
    first = re.split(r"(?<=[.!?])\s+(?=[A-Z`*(\[])", chosen, maxsplit=1)[0].strip().rstrip(".")
    if len(first) > limit:
        cut = first[: limit - 1]
        first = (cut[: cut.rfind(" ")] if " " in cut[20:] else cut).rstrip(" ,;:") + "…"
    return first


# ---- rendering -------------------------------------------------------------------------

def period_label(window: Window) -> str:
    a, b = window.start, window.period_to
    if (a.year, a.month) == (b.year, b.month):
        return f"{a.day}–{b.day} {b:%b %Y}"
    if a.year == b.year:
        return f"{a.day} {a:%b} – {b.day} {b:%b %Y}"
    return f"{a.day} {a:%b %Y} – {b.day} {b:%b %Y}"


def window_of(row: sqlite3.Row) -> Window:
    return Window(date.fromisoformat(row["period_from"]), date.fromisoformat(row["period_to"]) + timedelta(days=1))


def page_url(settings: DigestSettings, slug: str) -> str:
    return f"{settings.site_url}{slug}/" if settings.site_url else ""


def grouped(releases: list[sqlite3.Row]) -> "OrderedDict[tuple[str, str], list[sqlite3.Row]]":
    """Releases by (source, package), packages in name order, versions oldest first."""
    out: OrderedDict[tuple[str, str], list[sqlite3.Row]] = OrderedDict()
    for row in sorted(releases, key=lambda r: (r["source"] != "pypi", r["project"], r["released_at"])):
        out.setdefault((row["source"], row["project"]), []).append(row)
    return out


def notable(store: Store, releases: list[sqlite3.Row]) -> list[tuple[sqlite3.Row, str, bool]]:
    """(release, highlight, suggested) for the releases worth naming, best first."""
    scored = []
    for row in releases:
        line = highlight(row["notes"], row["summary"])
        if not line:
            continue
        earlier = [v for v in store.seen_versions(row["project"]) if v != row["version"]]
        suggested = suggest_headline(row["version"], earlier, row["notes"])
        parsed = parse_version(row["version"]) or (0, 0, 0)
        patch = parsed[2] != 0 or is_prerelease(row["version"])
        scored.append(((not suggested, patch, row["released_at"][::-1]), row, line, suggested))
    scored.sort(key=lambda item: item[0])
    seen_projects: set[str] = set()
    out = []
    for _key, row, line, suggested in scored:
        if row["project"] in seen_projects:  # one line per package
            continue
        seen_projects.add(row["project"])
        out.append((row, line, suggested))
    return out


def _yaml(value: str) -> str:
    """A YAML double-quoted scalar. JSON string syntax is valid YAML."""
    return json.dumps(value, ensure_ascii=False)


_PLAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def render_page(
    store: Store, settings: DigestSettings, window: Window, releases: list[sqlite3.Row], *, kind: str = "digest",
    title: str | None = None,
) -> str:
    groups = grouped(releases)
    count, packages = len(releases), len(groups)
    period = period_label(window)
    title = title or f"Digest: {count} release{'s' if count != 1 else ''} across {packages} package{'s' if packages != 1 else ''}"
    summary = f"{count} release{'s' if count != 1 else ''} across {packages} package{'s' if packages != 1 else ''}, {period}."
    lines = [
        "---",
        f"title: {_yaml(title)}",
        f"date: {window.end.isoformat()}",
        f"kind: {kind}",
        f"summary: {_yaml(summary[:300])}",
    ]
    if kind == "digest":
        lines.append(f"period: {{ from: {window.start.isoformat()}, to: {window.period_to.isoformat()} }}")
    lines.append("releases:")
    for (source, package), rows in groups.items():
        name = package if _PLAIN.match(package) else _yaml(package)
        versions = json.dumps([row["version"] for row in rows])
        source_part = "" if source == "pypi" else f", source: {source}"
        lines.append(f"  - {{ package: {name}{source_part}, versions: {versions} }}")
    lines += ["draft: true", "---", ""]
    lines.append(
        "<!-- Drafted by pip-release. Rewrite this into prose: what changed and why it matters. "
        "The release table is rendered from `releases`. Set draft: false when it's done. -->"
    )
    lines.append("")
    picks = notable(store, releases)
    if picks:
        lines.append("## Highlights")
        lines.append("")
        for row, line, suggested in picks:
            mark = " *(suggested headline)*" if suggested else ""
            lines.append(f"- **{row['project']} {row['version']}**: {line}{mark}")
        lines.append("")
    return "\n".join(lines)


def social_text(window: Window, releases: list[sqlite3.Row], picks: list[tuple[sqlite3.Row, str, bool]], n: int) -> str:
    count, packages = len(releases), len(grouped(releases))
    head = (
        f"Argus digest, {period_label(window)}: {count} release{'s' if count != 1 else ''} "
        f"across {packages} package{'s' if packages != 1 else ''}."
    )
    bullets = [
        f"• {row['project'].removeprefix('argus-')} {row['version']}: {line.replace('`', '')}"
        for row, line, _ in picks[:n]
    ]
    return "\n\n".join([head, "\n".join(bullets)]) if bullets else head


def render_for(publisher: Publisher, text_for, link: str, title: str) -> Rendered:
    """The longest variant (3, 2, 1, 0 highlights) that fits the platform without clipping."""
    for n in range(MAX_HIGHLIGHTS, -1, -1):
        text = text_for(n)
        rendered = publisher.render_post(Post(kind="digest", subject=title, text=text, link=link, title=title))
        if text in rendered.body:
            return rendered
    return rendered


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_page(
    store: Store, settings: DigestSettings, digest_id: int, slug: str, text: str, gate: Gate | None
) -> str:
    """Write the site page when it is safe to. Returns what happened, for the reviewer."""
    if settings.site_dir is None:
        return "no digest.site_dir configured; page not written"
    if gate is None:
        store.update_digest(digest_id, error="privacy gate unavailable; page not written")
        return "privacy gate unavailable; page not written"
    hits = gate.check({"site page": text})
    if hits:
        store.update_digest(digest_id, error=describe(hits))
        return "refused by the privacy gate; page not written"
    path = settings.site_dir / f"{slug}.md"
    row = store.digest(digest_id)
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if current == text:
            store.update_digest(digest_id, site_path=str(path), site_hash=_sha(text), error=None)
            return f"unchanged: {path}"
        if not (row and row["site_hash"] and _sha(current) == row["site_hash"]):
            store.update_digest(digest_id, site_path=str(path), error=None)
            return f"left alone (edited by a person): {path}"
    if not settings.site_dir.is_dir():
        store.update_digest(digest_id, error=f"site_dir {settings.site_dir} does not exist")
        return f"site_dir {settings.site_dir} does not exist; page not written"
    path.write_text(text, encoding="utf-8")
    store.update_digest(digest_id, site_path=str(path), site_hash=_sha(text), error=None)
    return f"wrote {path}"


def build_digest(
    store: Store, settings: DigestSettings, publishers: list[Publisher], digest_id: int, gate: Gate | None
) -> tuple[list[int], str]:
    """Create or refresh the digest's drafts and site page. Returns (new draft ids, page status).

    Drafts a person has edited, and pages a person has edited, are left alone.
    """
    row = store.digest(digest_id)
    if row is None:
        raise ValueError(f"No digest {digest_id}")
    window = window_of(row)
    included = [r for r in store.releases(digest_id=digest_id) if not r["excluded"]]
    if not included:
        return [], "no releases in this window; nothing drafted"
    picks = notable(store, included)
    link = page_url(settings, window.slug)
    subject = f"Digest {period_label(window)}"
    targets = [p for p in publishers if not settings.platforms or p.id in settings.platforms]
    post_key = f"digest:{window.start.isoformat()}"
    existing = {d.platform: d for d in store.list_drafts(post_key=post_key)}
    created: list[int] = []
    for publisher in targets:
        rendered = render_for(publisher, lambda n: social_text(window, included, picks, n), link, subject)
        if publisher.id in existing:
            store.refresh_text(existing[publisher.id].id, rendered.title, rendered.body)
            continue
        draft_id, made = store.add_draft(
            kind="digest", post_key=post_key, subject=subject, platform=publisher.id,
            title=rendered.title, body=rendered.body, link=link,
        )
        if made:
            created.append(draft_id)
    page = render_page(store, settings, window, included)
    status = write_page(store, settings, digest_id, window.slug, page, gate)
    return created, status


def promote(
    store: Store, settings: DigestSettings | None, publishers: list[Publisher], row: sqlite3.Row,
    *, site_page: bool = False, gate: Gate | None = None,
) -> tuple[list[int], str]:
    """Give one release its own post group (kind headline), and optionally a site page."""
    release = Release(
        project=row["project"], version=row["version"], summary=row["summary"], url=row["url"],
        author=None, project_urls={}, yanked=False, notes=row["notes"],
    )
    post_key = f"headline:{row['project']}=={row['version']}"
    created: list[int] = []
    for publisher in publishers:
        rendered = publisher.render(release)
        draft_id, made = store.add_draft(
            kind="headline", post_key=post_key, subject=f"{row['project']} {row['version']}",
            project=row["project"], version=row["version"], platform=publisher.id,
            title=rendered.title, body=rendered.body, link=row["url"],
        )
        if made:
            created.append(draft_id)
    status = ""
    if site_page and settings is not None:
        day = datetime.now(timezone.utc).date()
        window = Window(day, day + timedelta(days=1))
        slug = f"{day.isoformat()}-{row['project']}-{row['version'].replace('.', '-')}"
        text = render_page(store, settings, window, [row], kind="release", title=f"{row['project']} {row['version']}")
        status = _write_release_page(settings, slug, text, gate)
    return created, status


def _write_release_page(settings: DigestSettings, slug: str, text: str, gate: Gate | None) -> str:
    if settings.site_dir is None:
        return "no digest.site_dir configured; page not written"
    if gate is None:
        return "privacy gate unavailable; page not written"
    hits = gate.check({"site page": text})
    if hits:
        return describe(hits)
    path = Path(settings.site_dir) / f"{slug}.md"
    if path.exists():
        return f"left alone (already exists): {path}"
    path.write_text(text, encoding="utf-8")
    return f"wrote {path}"
