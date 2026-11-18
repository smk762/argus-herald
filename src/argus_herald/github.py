"""GitHub Releases as a release source, for repos that ship without PyPI (demo UIs, Studio).

With GITHUB_TOKEN the REST API is used (it says which releases are drafts or prereleases).
Without it, the public `releases.atom` feed is read; it never lists drafts, and a prerelease
is recognised from its version.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import quote

from argus_herald.feeds import parse_atom
from argus_herald.http import Http, HttpError

API = "https://api.github.com"
MAX_PAGES = 50  # 100 releases a page
_VERSION = re.compile(r"^[vV]?(\d+\.\d+(?:\.\d+)?(?:[-.]?[A-Za-z0-9.]+)?)$")


@dataclass(frozen=True)
class GitHubRelease:
    repo: str
    tag: str
    version: str
    name: str
    notes: str  # Markdown (API) or text converted from HTML (feed)
    url: str
    published_at: str
    prerelease: bool


def version_from_tag(tag: str) -> str | None:
    """`v0.4.0` -> `0.4.0`. Tags that aren't versions (`stock-heads`) give None."""
    match = _VERSION.match(tag.strip())
    return match.group(1) if match else None


def list_releases(http: Http, owner: str, repo: str, token: str = "") -> list[GitHubRelease]:
    """Published releases, oldest first. Drafts are never returned."""
    from argus_herald.digest import is_prerelease

    out: list[GitHubRelease] = []
    if token:
        items: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            url = f"{API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/releases?per_page=100&page={page}"
            status, body = http.request(
                "GET",
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Authorization": f"Bearer {token}",
                },
            )
            if status != 200:
                raise HttpError("GET", url, status)
            batch = json.loads(body.decode("utf-8"))
            if not isinstance(batch, list):
                raise ValueError("Unexpected GitHub response")
            items.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                break
        for item in items:
            if item.get("draft"):
                continue
            version = version_from_tag(str(item.get("tag_name") or ""))
            if not version:
                continue
            out.append(
                GitHubRelease(
                    repo=repo,
                    tag=str(item["tag_name"]),
                    version=version,
                    name=str(item.get("name") or ""),
                    notes=str(item.get("body") or ""),
                    url=str(item.get("html_url") or ""),
                    published_at=str(item.get("published_at") or item.get("created_at") or "").replace("Z", "+00:00"),
                    prerelease=bool(item.get("prerelease")) or is_prerelease(version),
                )
            )
    else:
        url = f"https://github.com/{quote(owner, safe='')}/{quote(repo, safe='')}/releases.atom"
        status, body = http.request("GET", url, headers={"Accept": "application/atom+xml"})
        if status != 200:
            raise HttpError("GET", url, status)
        for item in parse_atom(body):
            tag = item.link.rstrip("/").rsplit("/tag/", 1)[-1] if "/tag/" in item.link else ""
            version = version_from_tag(html.unescape(tag))
            if not version:
                continue
            out.append(
                GitHubRelease(
                    repo=repo,
                    tag=tag,
                    version=version,
                    name=item.title,
                    notes=html_to_text(item.content),
                    url=item.link,
                    published_at=item.published.replace("Z", "+00:00"),
                    prerelease=is_prerelease(version),
                )
            )
    out.sort(key=lambda r: r.published_at)
    return out


class _Text(HTMLParser):
    """Rough HTML to Markdown-ish text: headings, list items and paragraphs keep their shape."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in {"p", "div"} and self.parts and self.parts[-1] == "\n- ":
            pass  # <li><p>text</p></li>: keep the text on the bullet line
        elif tag in {"p", "div", "ul", "ol", "pre"}:
            self.parts.append("\n\n")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "code":
            self.parts.append("`")

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag == "code":
            self.parts.append("`")
        elif tag in {"ul", "ol", "pre"} or tag.startswith("h") and len(tag) == 2:
            self.parts.append("\n")

    def handle_data(self, data):  # noqa: ANN001
        self.parts.append(data)


def html_to_text(markup: str) -> str:
    if not markup.strip():
        return ""
    parser = _Text()
    parser.feed(markup)
    parser.close()  # flushes text held back as a possible entity or tag, such as a body ending in "R&D"
    text = "".join(parser.parts)
    lines = [line.rstrip() for line in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
