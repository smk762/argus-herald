"""YAML or JSON configuration. Secrets stay in the environment."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pip_release.feeds import FeedSource, youtube_feed_url

_SECRET_KEY = re.compile(
    r"(secret|token|password|webhook|api[_-]?key|access[_-]?key|credential)",
    re.IGNORECASE,
)


class ConfigError(Exception):
    pass


@dataclass
class Destination:
    enabled: bool = True
    username: str | None = None
    name: str | None = None
    url: str | None = None
    mode: str | None = None  # twitter: "manual" (default) or "api"


MODES = ("manual", "api")


@dataclass
class Config:
    users: list[str]
    projects: list[str]
    ignore: list[str]
    destinations: dict[str, Destination]
    interval_seconds: int = 1800
    store_path: Path = field(default_factory=lambda: Path(".pip-release/state.sqlite"))
    path: Path | None = None
    privacy: dict[str, Any] = field(default_factory=dict)
    feeds: list[FeedSource] = field(default_factory=list)
    discover_owner: str | None = None  # GitHub user or org whose repos are checked by discover
    discover_prefix: str = ""
    digest: "DigestSettings | None" = None  # None: one draft per release (per_release mode)
    github: "GitHubSource | None" = None
    media_dir: Path | None = None  # where attachments and their cleaned copies live

    @property
    def media_root(self) -> Path:
        return self.media_dir or self.store_path.parent / "media"


@dataclass(frozen=True)
class GitHubSource:
    """Releases published on GitHub, for repos that don't ship to PyPI (or as better notes)."""

    owner: str
    repos: tuple[str, ...] = ()  # explicit list; empty means every public repo matching prefix
    prefix: str = ""
    include_prereleases: bool = True  # collected into digests, never drafted alone


@dataclass(frozen=True)
class DigestSettings:
    """Digest mode: releases are collected into windows instead of drafted one by one."""

    anchor: date  # first day of the first window (UTC)
    window_days: int = 14
    site_url: str = ""  # where the site serves update pages, e.g. https://dragonhound.dev/updates/
    site_dir: Path | None = None  # local checkout's content dir; herald writes pages there
    platforms: tuple[str, ...] = ()  # empty: every enabled destination


def resolve_config_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("PIP_RELEASE_CONFIG")
    if env:
        return Path(env)
    return Path("config.yaml")


def load_config(path: Path) -> Config:
    if not path.is_file():
        raise ConfigError(
            f"No config at {path}. Copy config.example.yaml to config.yaml, "
            "or pass --example dragonhound to init."
        )
    data = _read(path)
    if not isinstance(data, dict):
        raise ConfigError("Config must be a mapping")
    _reject_secrets(data, str(path))
    pypi = data.get("pypi") or {}
    if not isinstance(pypi, dict):
        raise ConfigError("pypi must be a mapping")
    users = _strings(pypi.get("users") or [])
    single = pypi.get("user")
    if isinstance(single, str) and single.strip():
        users.insert(0, single.strip())
    projects = _dedupe(_strings(pypi.get("projects") or []))
    ignore = _strings(pypi.get("ignore") or [])
    discover = pypi.get("discover") or {}
    if not isinstance(discover, dict):
        raise ConfigError("pypi.discover must be a mapping")
    owner = _optional_str(discover.get("github_owner"))
    prefix = discover.get("prefix") or ""
    if not isinstance(prefix, str):
        raise ConfigError("pypi.discover.prefix must be a string")
    destinations = _destinations(data.get("destinations") or {})
    poll = data.get("poll") or {}
    store = data.get("store") or {}
    privacy = data.get("privacy") or {}
    if not isinstance(poll, dict) or not isinstance(store, dict) or not isinstance(privacy, dict):
        raise ConfigError("poll, store and privacy must be mappings")
    interval = int(poll.get("interval_seconds") or 1800)
    if interval < 60:
        interval = 60
    store_path = Path(str(store.get("path") or ".pip-release/state.sqlite"))
    env_store = os.environ.get("PIP_RELEASE_STORE")
    if env_store:
        store_path = Path(env_store)
    feeds = _feeds(data.get("sources") or {})
    unknown = sorted({p for feed in feeds for p in feed.platforms} - set(destinations))
    if unknown:
        raise ConfigError(f"sources name platforms that aren't in destinations: {', '.join(unknown)}")
    github = _github(data.get("github"))
    media = data.get("media") or {}
    if not isinstance(media, dict) or not isinstance(media.get("dir", ""), str):
        raise ConfigError("media must be a mapping with an optional dir path")
    digest = _digest(data.get("digest"))
    if digest:
        unknown = sorted(set(digest.platforms) - set(destinations))
        if unknown:
            raise ConfigError(f"digest.platforms aren't in destinations: {', '.join(unknown)}")
    return Config(
        media_dir=Path(media["dir"]).expanduser() if media.get("dir") else None,
        github=github,
        digest=digest,
        feeds=feeds,
        discover_owner=owner.strip() if owner else None,
        discover_prefix=prefix.strip(),
        users=users,
        projects=projects,
        ignore=ignore,
        destinations=destinations,
        interval_seconds=interval,
        store_path=store_path,
        path=path,
        privacy=privacy,
    )


def load_env_file(path: Path, environ: dict[str, str] | None = None) -> None:
    """Load KEY=VALUE lines. Existing variables are left alone."""

    target = os.environ if environ is None else environ
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in target:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        target[key] = value


def _read(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("Install PyYAML to read YAML config files") from exc
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc


def _reject_secrets(value: Any, where: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key)):
                raise ConfigError(
                    f"{where} key {key!r} looks like a secret. Put it in .env instead."
                )
            _reject_secrets(child, where)
    elif isinstance(value, list):
        for child in value:
            _reject_secrets(child, where)


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError("Expected a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError("Expected a list of strings")
        out.append(item.strip())
    return out


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _destinations(value: Any) -> dict[str, Destination]:
    if not isinstance(value, dict):
        raise ConfigError("destinations must be a mapping")
    out: dict[str, Destination] = {}
    for key, raw in value.items():
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError(f"destinations.{key} must be a mapping")
        url = raw.get("url")
        if url is not None and not isinstance(url, str):
            raise ConfigError(f"destinations.{key}.url must be a string")
        mode = raw.get("mode")
        if mode is not None and mode not in MODES:
            raise ConfigError(f"destinations.{key}.mode must be one of {', '.join(MODES)}")
        out[str(key)] = Destination(
            enabled=bool(raw.get("enabled", True)),
            username=_optional_str(raw.get("username")),
            name=_optional_str(raw.get("name")),
            url=url.strip() if isinstance(url, str) else None,
            mode=mode,
        )
    return out


def _github(value: Any) -> GitHubSource | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError("github must be a mapping")
    owner = value.get("owner")
    if not isinstance(owner, str) or not re.match(r"^[A-Za-z0-9-]{1,39}$", owner.strip()):
        raise ConfigError("github.owner must be a GitHub user or organisation name")
    repos = tuple(_strings(value.get("repos") or []))
    prefix = value.get("prefix") or ""
    if not isinstance(prefix, str):
        raise ConfigError("github.prefix must be a string")
    if not repos and not prefix:
        raise ConfigError("github needs repos: [...] or a prefix")
    pre = value.get("include_prereleases", True)
    if not isinstance(pre, bool):
        raise ConfigError("github.include_prereleases must be true or false")
    return GitHubSource(owner=owner.strip(), repos=repos, prefix=prefix.strip(), include_prereleases=pre)


def _digest(value: Any) -> DigestSettings | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError("digest must be a mapping")
    mode = value.get("mode", "digest")
    if mode not in ("digest", "per_release"):
        raise ConfigError("digest.mode must be digest or per_release")
    if mode == "per_release":
        return None
    raw_anchor = value.get("anchor")
    try:
        anchor = raw_anchor if isinstance(raw_anchor, date) else date.fromisoformat(str(raw_anchor))
    except ValueError as exc:
        raise ConfigError("digest.anchor must be a date like 2026-11-16") from exc
    days = value.get("window_days", 14)
    if not isinstance(days, int) or not 1 <= days <= 92:
        raise ConfigError("digest.window_days must be a whole number of days, 1 to 92")
    url = value.get("site_url") or ""
    if not isinstance(url, str) or (url and not url.startswith("https://")):
        raise ConfigError("digest.site_url must be an https:// URL")
    site_dir = value.get("site_dir")
    if site_dir is not None and not isinstance(site_dir, str):
        raise ConfigError("digest.site_dir must be a path")
    return DigestSettings(
        anchor=anchor,
        window_days=days,
        site_url=(url.rstrip("/") + "/") if url else "",
        site_dir=Path(site_dir).expanduser() if site_dir else None,
        platforms=tuple(_strings(value.get("platforms") or [])),
    )


def _feeds(value: Any) -> list[FeedSource]:
    """`sources.site` (an Atom feed of the site's updates) and `sources.youtube` (a channel)."""
    if not isinstance(value, dict):
        raise ConfigError("sources must be a mapping")
    unknown = sorted(set(value) - {"site", "youtube"})
    if unknown:
        raise ConfigError(f"Unknown sources: {', '.join(unknown)}. Known: site, youtube")
    feeds: list[FeedSource] = []
    for name, raw in value.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"sources.{name} must be a mapping")
        if raw.get("enabled") is False:
            continue
        platforms = tuple(_strings(raw.get("platforms") or []))
        if name == "site":
            url = raw.get("feed")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ConfigError("sources.site.feed must be an https:// Atom feed URL")
            categories = tuple(c.lower() for c in _strings(raw.get("categories") or ["note"]))
            feeds.append(FeedSource("site", url.strip(), "note", categories, platforms))
        else:
            channel = raw.get("channel_id")
            if not isinstance(channel, str):
                raise ConfigError("sources.youtube.channel_id must be a string")
            try:
                url = youtube_feed_url(channel.strip())
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            feeds.append(FeedSource("youtube", url, "video", (), platforms))
    return feeds


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("Expected a string")
    text = value.strip()
    return text or None
