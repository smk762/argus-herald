"""Plain-text fitting for platform limits."""

from __future__ import annotations

import html
import re
import unicodedata

_URL = re.compile(r"https?://[^\s]+")
_HEADING = re.compile(r"^##\s+", re.MULTILINE)


# One emoji as X counts it: a flag, a keycap, or a pictograph with its modifiers and ZWJ joins.
_PICTO = (
    "[\u00a9\u00ae\u203c\u2049\u2122\u2139\u2194-\u21aa\u231a-\u23ff\u24c2\u25aa-\u25fe\u2600-\u27bf"
    "\u2934\u2935\u2b00-\u2bff\u3030\u303d\u3297\u3299\U0001f000-\U0001faff]"
)
_PICTO_ONE = rf"{_PICTO}(?:\ufe0f|[\U0001f3fb-\U0001f3ff])?\u20e3?(?:[\U000e0020-\U000e007e]+\U000e007f)?"
_EMOJI = re.compile(
    rf"[\U0001f1e6-\U0001f1ff]{{2}}|[0-9#*]\ufe0f?\u20e3|{_PICTO_ONE}(?:\u200d{_PICTO_ONE})*",
)
# The code points X weighs as one character (twitter-text config v3); everything else is two.
_LIGHT = ((0x0000, 0x10FF), (0x2000, 0x200D), (0x2010, 0x201F), (0x2032, 0x2037))


def _weight(text: str) -> int:
    total = 0
    pos = 0
    for match in _EMOJI.finditer(text):
        total += _weight_chars(text[pos : match.start()]) + 2
        pos = match.end()
    return total + _weight_chars(text[pos:])


def _weight_chars(text: str) -> int:
    return sum(1 if any(lo <= ord(char) <= hi for lo, hi in _LIGHT) else 2 for char in text)


_JOINERS = "\u200d\ufe0e\ufe0f"


def _clusters(text: str) -> list[str]:
    """Split into the units a reader sees as one character: an emoji sequence (flag, keycap, ZWJ
    family), or a character with the combining marks, variation selectors and joiners after it."""
    out: list[str] = []
    pos = 0

    def chars(run: str) -> None:
        for char in run:
            if out and (char in _JOINERS or unicodedata.category(char) in ("Mn", "Mc", "Me")):
                out[-1] += char
            else:
                out.append(char)

    for match in _EMOJI.finditer(text):
        chars(text[pos : match.start()])
        out.append(match.group())
        pos = match.end()
    chars(text[pos:])
    return out


def weighted_length(text: str) -> int:
    """The length X counts: a URL is 23 (the t.co length), an emoji 2, CJK and most symbols 2."""

    text = unicodedata.normalize("NFC", text)
    total = 0
    pos = 0
    for match in _URL.finditer(text):
        total += _weight(text[pos : match.start()])
        total += 23
        pos = match.end()
    total += _weight(text[pos:])
    return total


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    if limit < 4:
        return ""
    return text[: limit - 3].rstrip() + "..."


def clip_lines(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    suffix = "\n..."
    if limit <= len(suffix):
        return ""
    cut = text[: limit - len(suffix)]
    newline = cut.rfind("\n")
    if newline > limit // 2:
        cut = cut[:newline]
    return cut.rstrip() + suffix


def fit_body(head: str, notes: str, link: str, limit: int) -> str:
    """Join a headline, optional notes, and a link without dropping the link."""

    head = " ".join(head.split())
    notes = notes.strip()
    link = link.strip()
    base = "\n\n".join(part for part in (head, link) if part)
    if len(base) > limit:
        room = limit - (len(link) + 2 if link else 0)
        head = clip(head, max(room, 0))
        return "\n\n".join(part for part in (head, link) if part)[:limit]
    if not notes:
        return base
    room = limit - len(base) - 2
    if room < 8:
        return base
    trimmed = clip_lines(notes, room)
    body = "\n\n".join(part for part in (head, trimmed, link) if part)
    if len(body) <= limit:
        return body
    return base


def fit_weighted(head: str, link: str, limit: int = 280) -> str:
    head = " ".join(head.split())
    link = link.strip()
    sep = "\n\n"

    def fits(text: str) -> bool:
        return weighted_length(text) <= limit

    if not head:
        return link
    body = head + sep + link
    if fits(body):
        return body
    parts = _clusters(head)  # cut between whole characters: half an emoji or a bare accent reads as broken
    while head and not fits(head + sep + link):
        parts.pop()
        head = "".join(parts).rstrip()
    if head:
        space = head.rfind(" ")
        if space > 20:
            shorter = head[:space].rstrip()
            if shorter and fits(shorter + sep + link):
                head = shorter
        return head + sep + link
    return link


def html_text(text: str) -> str:
    return html.escape(text, quote=False)


def plain_to_html(text: str) -> str:
    paragraphs = [part.strip() for part in text.strip().split("\n\n") if part.strip()]
    return "\n".join("<p>" + html_text(part).replace("\n", "<br>\n") + "</p>" for part in paragraphs)


def extract_changelog(markdown: str, version: str) -> str:
    """Return the Keep a Changelog section for ``version``, without the heading."""

    # The version must end where it ends: 1.2 is not the heading of 1.2.1, 1.20 or 1.2-rc1.
    v = re.escape(version)
    heading = re.compile(
        rf"^##\s+(?:\[{v}\]|{v}(?![\w.+\-])).*$",
        re.MULTILINE,
    )
    match = heading.search(markdown)
    if match is None:
        return ""
    rest = markdown[match.end() :]
    nxt = _HEADING.search(rest)
    section = rest[: nxt.start()] if nxt else rest
    return section.strip()
