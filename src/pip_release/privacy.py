"""Privacy gate: refuse drafts that would broadcast something private.

Runs on approve and again on publish. A hit refuses the draft. Text is never rewritten:
the reviewer edits it, or accepts one specific hit for one draft with a note.

The built-in checks follow the ai-code-auditor public-export gate (smk762/ai-code-auditor,
wiki/public.py): private or link-local IPs, home and mount paths, email addresses, LAN
hostnames and a private deny-list of literal terms. Herald adds crypto addresses (with an
allow-list for known public contracts), timezone offsets attached to timestamps, and
regexes from config.

The deny-list is a private file, one term per line, ``#`` comments allowed. It lives outside
any repository and is never committed. Without it the gate cannot be built, and approve and
publish are refused (fail closed).
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

DENYLIST_ENV = "PIP_RELEASE_OPSEC_DENYLIST"
DEFAULT_DENYLIST = Path.home() / ".config" / "pip-release" / "opsec-denylist.txt"
# The auditor keeps its own list. Terms added to either list are enforced by both tools.
AUDITOR_DENYLIST_ENV = "AI_AUDIT_OPSEC_DENYLIST"
AUDITOR_DENYLIST = Path.home() / ".config" / "ai-code-auditor" / "opsec-denylist.txt"

# An address may end a sentence ("... at 192.168.1.9."), so only a dot followed by a digit
# means "part of a longer dotted number".
_IPV4 = re.compile(r"(?<!\d)(?<!\d\.)(\d{1,3}(?:\.\d{1,3}){3})(?!\d)(?!\.\d)")
_IPV6_LOCAL = re.compile(r"(?i)\b(fe80|fc[0-9a-f]{2}|fd[0-9a-f]{2}):[0-9a-f:]+")
_PATHS = re.compile(
    r"(?<![\w.])(/home/[\w.-]+|/Users/[\w.-]+|/root/|/mnt/[\w.-]+|/media/[\w.-]+|[A-Za-z]:\\Users\\)"
)
_EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+\.)+[A-Za-z]{2,}\b")
_EMAIL_OK = re.compile(r"(?i)@(example\.(com|org|net)|[\w.-]+\.invalid|users\.noreply\.github\.com)$")
_LAN_HOST = re.compile(r"(?i)\b[a-z0-9-]+\.(lan|local|internal|home\.arpa|localdomain)\b")
_EVM = re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{40}(?![0-9A-Za-z])")
_BASE58 = re.compile(r"(?<![0-9A-Za-z])[1-9A-HJ-NP-Za-km-z]{26,35}(?![0-9A-Za-z])")
_BECH32 = re.compile(r"(?i)(?<![0-9a-z])(bc|ltc|tb)1[02-9ac-hj-np-z]{11,71}(?![0-9a-z])")
_ENS = re.compile(r"(?i)(?<![\w.-])[a-z0-9-]{3,}\.eth(?![\w.-])")
# A clock time followed by a non-zero numeric offset: "17:58:06 -0500", "10:00+05:30",
# "2026-11-15T09:00:00-05:00", git's default "17:58:06 2026 -0500", or "UTC+5" / "GMT-5".
# +00:00 and Z are fine.
_TZ_AFTER_TIME = re.compile(
    r"\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s\d{4})?\s?([+-])(\d{2}):?(\d{2})(?!\d)"
)
_TZ_NAMED = re.compile(r"\b(?:UTC|GMT)\s?[+-]\s?(\d{1,2})(?::?(\d{2}))?\b")


class GateUnavailable(Exception):
    """The gate can't be built, so nothing may be approved or published."""


@dataclass(frozen=True)
class Hit:
    rule: str
    label: str
    field: str
    start: int
    end: int
    match: str

    @property
    def key(self) -> str:
        """What an acceptance names: this rule and this exact text, case-insensitive."""
        return f"{self.rule}:{self.match.lower()}"


@dataclass
class Gate:
    deny_terms: list[str] = field(default_factory=list)
    patterns: dict[str, str] = field(default_factory=dict)
    allow_addresses: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._allowed = {item.strip().lower() for item in self.allow_addresses if item.strip()}
        self._terms = [
            (term, re.compile(rf"(?i)(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"))
            for term in self.deny_terms
        ]
        self._custom = []
        for name, pattern in self.patterns.items():
            try:
                self._custom.append((name, re.compile(pattern)))
            except re.error as exc:
                raise GateUnavailable(f"privacy.patterns.{name} is not a valid regex: {exc}") from exc

    def scan(self, text: str, field: str = "body") -> list[Hit]:
        """Every hit in the text, in order of position."""
        hits: list[Hit] = []

        def add(rule: str, label: str, start: int, end: int) -> None:
            hits.append(Hit(rule, label, field, start, end, text[start:end]))

        for m in _IPV4.finditer(text):
            try:
                ip = ipaddress.IPv4Address(m.group(1))
            except ValueError:
                continue
            if (ip.is_private or ip.is_link_local) and not ip.is_loopback and not ip.is_unspecified:
                add("private-ip", "private or link-local IP", m.start(1), m.end(1))
        for m in _IPV6_LOCAL.finditer(text):
            add("private-ip", "private or link-local IP", m.start(), m.end())
        for m in _PATHS.finditer(text):
            add("home-path", "home or mount path", m.start(), m.end())
        for m in _EMAIL.finditer(text):
            if not _EMAIL_OK.search(m.group(0)):
                add("email", "email address", m.start(), m.end())
        for m in _LAN_HOST.finditer(text):
            add("lan-host", "LAN hostname", m.start(), m.end())
        for m in _EVM.finditer(text):
            if m.group(0).lower() not in self._allowed:
                add("crypto-address", "crypto address", m.start(), m.end())
        for m in _BECH32.finditer(text):
            if m.group(0).lower() not in self._allowed:
                add("crypto-address", "crypto address", m.start(), m.end())
        for m in _BASE58.finditer(text):
            if _base58check(m.group(0)) and m.group(0).lower() not in self._allowed:
                add("crypto-address", "crypto address", m.start(), m.end())
        for m in _ENS.finditer(text):
            if m.group(0).lower() not in self._allowed:
                add("crypto-address", "ENS name", m.start(), m.end())
        for m in _TZ_AFTER_TIME.finditer(text):
            if m.group(2) != "00" or m.group(3) != "00":
                add("tz-offset", "timezone offset", m.start(1), m.end())
        for m in _TZ_NAMED.finditer(text):
            if int(m.group(1)) or (m.group(2) and int(m.group(2))):
                add("tz-offset", "timezone offset", m.start(), m.end())
        for term, pattern in self._terms:
            for m in pattern.finditer(text):
                add("deny-term", "deny-listed term", m.start(), m.end())
        for name, pattern in self._custom:
            for m in pattern.finditer(text):
                if m.end() > m.start():
                    add(f"pattern:{name}", f"pattern {name}", m.start(), m.end())
        hits.sort(key=lambda hit: (hit.start, -hit.end))
        return hits

    def check(self, fields: Mapping[str, str], accepted: Iterable[str] = ()) -> list[Hit]:
        """Hits across several fields that haven't been accepted for this draft."""
        allowed = set(accepted)
        found: list[Hit] = []
        for name, text in fields.items():
            found.extend(hit for hit in self.scan(text or "", name) if hit.key not in allowed)
        return found


def load_gate(
    privacy: Mapping[str, object] | None = None, env: Mapping[str, str] | None = None
) -> Gate:
    """Build the gate from config's `privacy` block and the private deny-list file(s)."""
    env = os.environ if env is None else env
    privacy = privacy or {}
    path = Path(env.get(DENYLIST_ENV) or DEFAULT_DENYLIST).expanduser()
    if not path.is_file():
        raise GateUnavailable(
            f"The privacy deny-list is missing ({path}). Approve and publish stay refused until it "
            f"exists: one private term per line, outside any repository. {DENYLIST_ENV} overrides the path."
        )
    warnings: list[str] = []
    terms = _read_terms(path, warnings)
    auditor = Path(env.get(AUDITOR_DENYLIST_ENV) or AUDITOR_DENYLIST).expanduser()
    if auditor.is_file() and auditor.resolve() != path.resolve():
        terms.extend(_read_terms(auditor, warnings))
    patterns = privacy.get("patterns") or {}
    allow = privacy.get("allow_addresses") or []
    if not isinstance(patterns, Mapping) or not all(isinstance(v, str) for v in patterns.values()):
        raise GateUnavailable("privacy.patterns must map names to regex strings")
    if not isinstance(allow, list) or not all(isinstance(v, str) for v in allow):
        raise GateUnavailable("privacy.allow_addresses must be a list of strings")
    return Gate(
        deny_terms=list(dict.fromkeys(terms)),
        patterns={str(k): v for k, v in patterns.items()},
        allow_addresses=list(allow),
        warnings=warnings,
    )


def describe(hits: list[Hit]) -> str:
    parts = [f"{hit.label} in {hit.field}: {hit.match!r} (accept as {hit.key!r})" for hit in hits]
    return "Refused by the privacy gate: " + "; ".join(parts)


def segments(text: str, hits: list[Hit]) -> list[tuple[str, Hit | None]]:
    """Split text into plain and hit pieces for highlighting. Overlapping hits keep the first."""
    out: list[tuple[str, Hit | None]] = []
    pos = 0
    for hit in sorted(hits, key=lambda h: (h.start, -h.end)):
        if hit.start < pos:
            continue
        if hit.start > pos:
            out.append((text[pos:hit.start], None))
        out.append((text[hit.start:hit.end], hit))
        pos = hit.end
    if pos < len(text):
        out.append((text[pos:], None))
    return out


def _read_terms(path: Path, warnings: list[str]) -> list[str]:
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            warnings.append(f"{path} is readable by other users. Run: chmod 600 {path}")
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GateUnavailable(f"Can't read the deny-list {path}: {exc}") from exc
    terms = [line.split("#", 1)[0].strip() for line in lines]
    return [term for term in terms if term]


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58check(value: str) -> bool:
    """True for a Base58Check string with a valid checksum (BTC, LTC, DOGE, KMD... addresses)."""
    number = 0
    for char in value:
        index = _B58.find(char)
        if index < 0:
            return False
        number = number * 58 + index
    pad = len(value) - len(value.lstrip("1"))
    raw = b"\0" * pad + number.to_bytes((number.bit_length() + 7) // 8, "big")
    if len(raw) < 5:
        return False
    body, checksum = raw[:-4], raw[-4:]
    return hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] == checksum
