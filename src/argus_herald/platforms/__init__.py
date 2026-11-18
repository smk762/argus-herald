"""Publishers. A new platform is a class plus an entry point."""

from __future__ import annotations

import copy
import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from importlib.metadata import entry_points
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from argus_herald.config import Audience, Config, Destination, Schedule
from argus_herald.http import Http, send_json
from argus_herald.legacy import LEGACY_PLATFORM_GROUP, warn_once
from argus_herald.media import PATREON_LIMITS, X_LIMITS, MediaLimits
from argus_herald.models import Draft, Post, PublishResult, Release, Rendered
from argus_herald.oauth1 import authorization_header
from argus_herald.textutil import fit_body, fit_weighted, html_text, weighted_length

log = logging.getLogger(__name__)

_ORDER = ("twitter", "discord", "telegram", "patreon")


class Publisher(ABC):
    id: str
    label: str
    # True when publish hands the text to a person instead of posting it.
    manual: bool = False
    # What a post can carry. None: this publisher can't attach media.
    media_limits: MediaLimits | None = None
    # Hashtags appended from the audience profile, at most this many.
    max_hashtags: int = 0
    audience: Audience | None = None
    schedule: Schedule | None = None

    def configure(self, destination: Destination | None) -> Publisher:
        """Return a publisher set up for this destination. This instance is left as it was, so
        setting up a second destination never changes the first."""
        publisher = copy.copy(self)
        publisher.audience = destination.audience if destination else None
        publisher.schedule = destination.schedule if destination else None
        return publisher

    def wants(self, packages: list[str]) -> bool:
        """Whether this destination's audience wants content about any of these packages.
        Content with no package (a video, a build note) goes everywhere."""
        if self.audience is None or not packages:
            return True
        return any(self.audience.wants(p) for p in packages)

    def fits(self, title: str, body: str) -> bool:
        try:
            self.validate(title, body)
        except ValueError:
            return False
        return True

    def shape(self, rendered: Rendered, link: str) -> tuple[Rendered, str]:
        """Apply the audience: move the link to a reply, add hashtags while they fit.
        Returns (rendered, reply text)."""
        audience = self.audience
        if audience is None:
            return rendered, ""
        body, reply = rendered.body, ""
        if audience.link_placement == "reply" and link and link in body:
            body = re.sub(r"\n*" + re.escape(link) + r"\s*", "\n", body).strip()
            reply = link
        tags = [f"#{t}" for t in audience.hashtags[: self.max_hashtags]]
        while tags:
            candidate = f"{body}\n\n{' '.join(tags)}"
            if self.fits(rendered.title, candidate):
                body = candidate
                break
            tags.pop()
        return Rendered(title=rendered.title, body=body), reply

    def handoff_url(self, draft: Draft) -> str | None:
        """Where a person posts a manual draft, if the platform has a prefilled link."""
        return None

    @abstractmethod
    def render(self, release: Release) -> Rendered: ...

    def render_post(self, post: Post) -> Rendered:
        """A post that isn't a release. Keeps paragraphs, clips the text, never drops the link."""
        body = fit_body("", post.text, post.link, getattr(self, "limit", 10000))
        return Rendered(title=post.title or post.subject, body=body)

    @abstractmethod
    def missing_secrets(self, env: Mapping[str, str]) -> list[str]: ...

    @abstractmethod
    def validate(self, title: str, body: str) -> None: ...

    @abstractmethod
    def publish(self, draft: Draft, env: Mapping[str, str], http: Http) -> PublishResult: ...


class LengthError(ValueError):
    pass


def _require(env: Mapping[str, str], names: tuple[str, ...]) -> list[str]:
    return [name for name in names if not (env.get(name) or "").strip()]


def _headline(release: Release) -> str:
    summary = release.summary.strip()
    head = f"{release.project} {release.version} is out."
    if summary:
        head = f"{head} {summary}"
    return head


def intent_url(text: str) -> str:
    """X's web intent: opens the composer with the text filled in. No API call, no cost."""
    return "https://x.com/intent/post?text=" + quote(text, safe="")


class TwitterPublisher(Publisher):
    """X. `manual` (default) hands off an intent link; `api` posts with OAuth 1.0a."""

    id = "twitter"
    label = "Twitter / X"
    limit = 280
    media_limits = X_LIMITS
    max_hashtags = 2

    def __init__(self, mode: str = "manual") -> None:
        self.mode = mode
        self.manual = mode == "manual"
        # The API path posts text only; media goes with the manual handoff.
        self.media_limits = X_LIMITS if self.manual else None

    def configure(self, destination: Destination | None) -> TwitterPublisher:
        mode = (destination.mode if destination else None) or "manual"
        publisher = copy.copy(self) if mode == self.mode else TwitterPublisher(mode)
        publisher.audience = destination.audience if destination else None
        publisher.schedule = destination.schedule if destination else None
        return publisher

    def reply_url(self, draft: Draft) -> str | None:
        """Intent for the self-reply that carries the link, once the post's URL is known."""
        # A confirmed handoff stores the post's URL; the API path stores the bare post id.
        external = (draft.external_id or "").strip()
        match = re.search(r"/status/(\d+)", external) or re.fullmatch(r"(\d+)", external)
        if not draft.reply or not match:
            return None
        return f"https://x.com/intent/post?in_reply_to={match.group(1)}&text=" + quote(draft.reply, safe="")

    def handoff_url(self, draft: Draft) -> str | None:
        return intent_url(draft.body)

    def render_post(self, post: Post) -> Rendered:
        text = post.text.strip()
        joined = "\n\n".join(part for part in (text, post.link.strip()) if part)
        if weighted_length(joined) <= self.limit:
            return Rendered(title="", body=joined)
        return Rendered(title="", body=fit_weighted(text, post.link, self.limit))

    def render(self, release: Release) -> Rendered:
        head = _headline(release)
        return Rendered(title="", body=fit_weighted(head, release.url, self.limit))

    def missing_secrets(self, env: Mapping[str, str]) -> list[str]:
        if self.manual:
            return []
        return _require(
            env,
            ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"),
        )

    def validate(self, title: str, body: str) -> None:
        if weighted_length(body) > self.limit:
            raise LengthError(f"Twitter posts must be {self.limit} weighted characters or fewer")

    def publish(self, draft: Draft, env: Mapping[str, str], http: Http) -> PublishResult:
        self.validate(draft.title, draft.body)
        if self.manual:
            return PublishResult(
                external_id=None,
                detail=(
                    "Open the link, attach any media and post it on X. Then run "
                    f"argus-herald confirm {draft.id} --url <post url>."
                ),
                handed_off=True,
            )
        missing = self.missing_secrets(env)
        if missing:
            raise LengthError(f"Missing {', '.join(missing)}")
        url = "https://api.x.com/2/tweets"
        header = authorization_header(
            "POST",
            url,
            env["X_API_KEY"],
            env["X_API_SECRET"],
            env["X_ACCESS_TOKEN"],
            env["X_ACCESS_TOKEN_SECRET"],
        )
        status, body = send_json(http, url, {"text": draft.body}, headers={"Authorization": header})
        if status not in (200, 201):
            raise LengthError(f"Twitter rejected the post with HTTP {status}")
        try:
            payload = json.loads(body.decode("utf-8"))
            external = str((payload.get("data") or {}).get("id") or "") or None
        except json.JSONDecodeError:
            external = None
        return PublishResult(external_id=external, detail="Posted to Twitter / X")


class DiscordPublisher(Publisher):
    id = "discord"
    label = "Discord"
    limit = 2000

    def render(self, release: Release) -> Rendered:
        head = _headline(release)
        body = fit_body(head, release.notes, release.url, self.limit)
        return Rendered(title=f"{release.project} {release.version}", body=body)

    def missing_secrets(self, env: Mapping[str, str]) -> list[str]:
        return _require(env, ("DISCORD_WEBHOOK_URL",))

    def validate(self, title: str, body: str) -> None:
        if len(body) > self.limit:
            raise LengthError(f"Discord posts must be {self.limit} characters or fewer")

    def publish(self, draft: Draft, env: Mapping[str, str], http: Http) -> PublishResult:
        missing = self.missing_secrets(env)
        if missing:
            raise LengthError("Missing DISCORD_WEBHOOK_URL")
        self.validate(draft.title, draft.body)
        url = _discord_url(env["DISCORD_WEBHOOK_URL"])
        # Release notes are other people's text: nothing in them may ping @everyone, a role or a user.
        status, body = send_json(http, url, {"content": draft.body, "allowed_mentions": {"parse": []}})
        if status not in (200, 204):
            raise LengthError(f"Discord rejected the post with HTTP {status}")
        external = None
        if body:
            try:
                external = str(json.loads(body.decode("utf-8")).get("id") or "") or None
            except json.JSONDecodeError:
                external = None
        return PublishResult(external_id=external, detail="Posted to Discord")


class TelegramPublisher(Publisher):
    id = "telegram"
    label = "Telegram"
    limit = 4096
    max_hashtags = 3

    def render(self, release: Release) -> Rendered:
        head = _headline(release)
        # Escaping can grow the text. Leave room for the common entities, then shrink the plain
        # budget until the escaped body fits: notes full of "&", "<" or "->" can outgrow any fixed margin.
        budget = 3500
        body = fit_body(head, release.notes, release.url, budget)
        while budget > 0 and (escaped := len(html_text(body))) > self.limit:
            budget = min(budget - 1, budget * self.limit // escaped)
            body = fit_body(head, release.notes, release.url, budget)
        return Rendered(title=f"{release.project} {release.version}", body=body)

    def missing_secrets(self, env: Mapping[str, str]) -> list[str]:
        return _require(env, ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"))

    def validate(self, title: str, body: str) -> None:
        if len(html_text(body)) > self.limit:
            raise LengthError(f"Telegram posts must be {self.limit} characters or fewer")

    def publish(self, draft: Draft, env: Mapping[str, str], http: Http) -> PublishResult:
        missing = self.missing_secrets(env)
        if missing:
            raise LengthError(f"Missing {', '.join(missing)}")
        self.validate(draft.title, draft.body)
        token = env["TELEGRAM_BOT_TOKEN"].strip()
        if "/" in token or not token:
            raise LengthError("TELEGRAM_BOT_TOKEN is not usable")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        status, body = send_json(
            http,
            url,
            {
                "chat_id": env["TELEGRAM_CHAT_ID"].strip(),
                "text": html_text(draft.body),
                "parse_mode": "HTML",
            },
        )
        if status != 200:
            raise LengthError(f"Telegram rejected the post with HTTP {status}")
        try:
            payload = json.loads(body.decode("utf-8"))
            result = payload.get("result") or {}
            external = str(result.get("message_id") or "") or None
        except (ValueError, AttributeError, TypeError):
            # The 200 means the message is posted. An odd body must not fail the publish: a retry would post twice.
            external = None
        return PublishResult(external_id=external, detail="Posted to Telegram")


class PatreonPublisher(Publisher):
    """Patreon's public API does not create posts. Publish hands the text to a person."""

    id = "patreon"
    label = "Patreon"
    limit = 10000
    manual = True
    media_limits = PATREON_LIMITS

    def render(self, release: Release) -> Rendered:
        head = _headline(release)
        body = fit_body(head, release.notes, release.url, self.limit)
        return Rendered(title=f"{release.project} {release.version}", body=body)

    def missing_secrets(self, env: Mapping[str, str]) -> list[str]:
        return []

    def validate(self, title: str, body: str) -> None:
        if len(title) > 300:
            raise LengthError("Patreon titles must be 300 characters or fewer")
        if len(body) > self.limit:
            raise LengthError(f"Patreon posts must be {self.limit} characters or fewer")

    def publish(self, draft: Draft, env: Mapping[str, str], http: Http) -> PublishResult:
        self.validate(draft.title, draft.body)
        return PublishResult(
            external_id=None,
            detail=(
                "Paste the post into the Patreon editor. "
                f"After it is published there, run argus-herald confirm {draft.id}."
            ),
            handed_off=True,
        )


def builtin_publishers() -> list[Publisher]:
    return [
        TwitterPublisher(),
        DiscordPublisher(),
        TelegramPublisher(),
        PatreonPublisher(),
    ]


def configured_publishers(config: Config) -> dict[str, Publisher]:
    """Every installed publisher by id, set up from its destination block when there is one."""
    return {publisher.id: publisher.configure(config.destinations.get(publisher.id)) for publisher in load_publishers()}


def load_publishers() -> list[Publisher]:
    found = {publisher.id: publisher for publisher in builtin_publishers()}
    extras = []
    for group in ("argus_herald.platforms", LEGACY_PLATFORM_GROUP):
        try:
            extras += [(group, item) for item in entry_points(group=group)]
        except Exception:
            continue
    for group, item in extras:
        if group == LEGACY_PLATFORM_GROUP:
            warn_once(
                f"ep:{item.name}",
                f"Platform {item.name!r} registers under {LEGACY_PLATFORM_GROUP}, which is "
                "deprecated and no longer loaded from 0.3: register it under argus_herald.platforms.",
            )
        try:
            obj = item.load()
            publisher = obj() if isinstance(obj, type) else obj
        except Exception:
            log.warning("Skipping platform entry point %s", item.name, exc_info=True)
            continue
        found[publisher.id] = publisher
    return sorted(
        found.values(),
        key=lambda publisher: (
            _ORDER.index(publisher.id) if publisher.id in _ORDER else 100,
            publisher.id,
        ),
    )


def _discord_url(raw: str) -> str:
    parsed = urlparse(raw.strip())
    if parsed.scheme != "https" or parsed.hostname not in {"discord.com", "discordapp.com"}:
        raise LengthError("DISCORD_WEBHOOK_URL must be a Discord webhook")
    if not parsed.path.startswith("/api/webhooks/"):
        raise LengthError("DISCORD_WEBHOOK_URL must be a Discord webhook")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["wait"] = "true"
    return urlunparse(parsed._replace(query=urlencode(query)))
