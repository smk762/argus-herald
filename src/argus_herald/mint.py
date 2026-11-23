"""argus-mint as a source: approved teasers become drafts, and their posts are reported back.

Herald pulls; mint never pushes. Each poll reads `GET /posts/ready`, and a teaser that is new,
current and not yet posted becomes one pending draft per image-capable destination, with mint's
PNG attached and its reviewed alt text. Drafts are keyed `mint:<draft id>`, and the source is
baselined on its first read, so nothing is drafted twice and nothing old is drafted at all.

When a teaser's post is live (a confirmed handoff with its URL, or a direct publish), a report
is queued in `mint_reports` and sent to `POST /drafts/{id}/posted` with the sha256 of the
cleaned copy herald posted. Mint only records a post of the exact image it reviewed, which is
why `media.clean` copies an already-bare PNG byte for byte. A report that can't be sent (mint is
on another network, say) stays queued and goes out on a later poll.

Mint's address is ARGUS_MINT_URL: in `.env`, or else in the suite settings file, where it is the
suite-wide name of the peer `mint` (`argus_cortex.peers`). The poster token, ARGUS_MINT_POSTER_TOKEN,
is herald's own secret and is read from `.env` only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from argus_cortex.peers import OFF, PeerRefused, peer_url

from argus_herald.config import Config
from argus_herald.http import BodyTooLarge, Http, HttpClient, HttpError, LanHttpClient, check_lan_url, send_json
from argus_herald.media import MediaError, clean
from argus_herald.models import APPROVED, FAILED, PENDING, PUBLISHED, Draft, Post
from argus_herald.platforms import Publisher
from argus_herald.store import Store
from argus_herald.workflow import PollEvent, ReviewError, attach_media, compose

log = logging.getLogger(__name__)

SOURCE = "mint"
KEY_PREFIX = "mint:"
PEER = "mint"
URL_ENV = "ARGUS_MINT_URL"  # also peer_env(PEER): herald's variable and the suite-wide one are the same name
TOKEN_ENV = "ARGUS_MINT_POSTER_TOKEN"
MAX_IMAGE_BYTES = 20_000_000
PAGE = 200
MAX_TEASERS = 5000
TIMEOUT = 10  # seconds; mint is often out of reach, and a confirm in the UI waits on this
_AWAY = "mint is unreachable; the report is sent on a later poll"  # expected, so never an error

# Matched with fullmatch: `$` alone would also accept a trailing newline. A draft id names a
# file under the media folder and goes into mint's URLs, so it is held to exactly this.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_WHERE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")


class MintError(Exception):
    pass


@dataclass(frozen=True)
class Teaser:
    id: str
    title: str
    number: str
    current: bool
    posted: bool
    image_url: str
    image_sha256: str
    alt: str
    quote: str
    attribution: str
    created_at: str

    @property
    def label(self) -> str:
        return " ".join(part for part in (self.title, self.number) if part) or self.id


@dataclass(frozen=True)
class Reported:
    """What happened to one queued report: sent, refused (for good) or waiting (tried again later)."""

    draft_id: int
    outcome: str
    detail: str = ""


def base_url(env: Mapping[str, str]) -> str:
    """Mint's address: ARGUS_MINT_URL in *env* (herald's `.env`), else the suite file's.

    `off` (or `none`) in *env* means mint is not configured here, whatever the suite file says.
    Either way the address is held to the same rule: https, or http to this machine or the LAN.
    """
    base = (env.get(URL_ENV) or "").strip().rstrip("/")
    if base.lower() in OFF:
        base = ""
    elif not base:
        try:
            base = peer_url(PEER) or ""  # blank here: the process environment, then the suite file
        except PeerRefused as exc:  # names the variable, never the value
            raise MintError(str(exc)) from None
    if not base:
        raise MintError(f"Set {URL_ENV} in .env (or the Argus suite file) to argus-mint's address")
    try:
        check_lan_url(base)
    except ValueError as exc:
        raise MintError(f"{URL_ENV} must be https://, or http:// to this machine or the LAN") from exc
    return base


def _client(http: Http) -> Http:
    """Mint usually runs on the LAN over plain http, which the default client refuses."""
    return (
        LanHttpClient(timeout=TIMEOUT) if isinstance(http, HttpClient) and not isinstance(http, LanHttpClient) else http
    )


def _text(value: object, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _teaser(raw: object) -> Teaser | None:
    """One feed item, or None when it isn't usable (mint's text is checked like any remote input)."""
    if not isinstance(raw, dict):
        return None
    teaser_id, sha, image = raw.get("id"), raw.get("image_sha256"), raw.get("image_url")
    if not isinstance(teaser_id, str) or not _ID.fullmatch(teaser_id):
        return None
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        return None
    # A path on mint itself: never another host, whatever the feed says. URL parsers drop tabs
    # and newlines, so "/\t/elsewhere" would read as "//elsewhere"; no control or space is let in.
    if not isinstance(image, str) or not image.startswith("/") or image.startswith("//") or "\\" in image:
        return None
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in image):
        return None
    return Teaser(
        id=teaser_id,
        title=" ".join(_text(raw.get("title"), 200).split()),
        number=_text(raw.get("number"), 40),
        current=raw.get("current") is True,
        posted=raw.get("posted") is not False,  # anything but a plain false counts as posted
        image_url=image,
        image_sha256=sha,
        alt=_text(raw.get("alt"), 2000),
        quote=_text(raw.get("quote"), 2000),
        attribution=_text(raw.get("attribution"), 200),
        created_at=_text(raw.get("created_at"), 40),
    )


def read_ready(http: Http, base: str, project: str = "") -> list[Teaser]:
    """Every teaser mint lists as approved or published, oldest first."""
    found: dict[str, Teaser] = {}
    offset = 0
    while offset < MAX_TEASERS:
        url = f"{base}/posts/ready?limit={PAGE}&offset={offset}"
        if project:
            url += f"&project={quote(project, safe='')}"
        status, body = http.request("GET", url, headers={"Accept": "application/json"})
        if status != 200:
            raise HttpError("GET", url, status)
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise MintError("mint's /posts/ready didn't answer with JSON") from exc
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise MintError("mint's /posts/ready has no items list")
        for raw in items:
            teaser = _teaser(raw)
            if teaser is not None:
                found.setdefault(teaser.id, teaser)
        offset += len(items)
        total = data.get("total")
        if not items or not isinstance(total, int) or offset >= total:
            break
    return sorted(found.values(), key=lambda teaser: teaser.created_at)


def fetch_image(http: Http, base: str, teaser: Teaser) -> bytes:
    """The teaser's PNG, checked against the hash mint's reviewers approved."""
    url = base + teaser.image_url
    headers = {"Accept": "image/png"}
    if isinstance(http, HttpClient):
        status, body = http.request("GET", url, headers=headers, limit=MAX_IMAGE_BYTES)
    else:
        status, body = http.request("GET", url, headers=headers)
    if status != 200:
        raise HttpError("GET", url, status)
    if len(body) > MAX_IMAGE_BYTES:
        raise BodyTooLarge("GET", url, MAX_IMAGE_BYTES)
    if hashlib.sha256(body).hexdigest() != teaser.image_sha256:
        raise MintError("the image isn't the one mint's feed describes (sha256 differs)")
    return body


def teaser_post(teaser: Teaser) -> Post:
    text = teaser.quote or teaser.title or teaser.label
    if teaser.quote and teaser.attribution:
        text = f"{text}\n— {teaser.attribution}"
    return Post(kind="teaser", subject=teaser.label, text=text, link="")


def poll_mint(
    config: Config,
    store: Store,
    http: Http,
    publishers: list[Publisher],
    env: Mapping[str, str],
    *,
    draft: bool = True,
) -> list[PollEvent]:
    """Send any queued reports, then read mint's feed. The first read baselines it."""
    source = config.mint
    if source is None:
        return []
    reports = send_reports(store, env, http)
    events = [
        PollEvent("error", SOURCE, detail=f"post of draft #{r.draft_id} not recorded: {r.detail}")
        for r in reports
        if r.outcome == "refused"
    ]
    # A report that waits on something a person has to fix (no token, a refused token, a failing
    # mint) is said once per cause; one that waits because mint is out of reach is expected.
    stuck = list(dict.fromkeys(r.detail for r in reports if r.outcome == "waiting" and r.detail != _AWAY))
    events += [PollEvent("error", SOURCE, detail=f"posts not reported to mint yet: {detail}") for detail in stuck]
    client = _client(http)
    try:
        base = base_url(env)
        teasers = read_ready(client, base, source.project)
    except HttpError as exc:
        if exc.status == 0:  # the laptop is off mint's network: expected, not a failure
            return [*events, PollEvent("skip", SOURCE, detail="unreachable; trying again on the next poll")]
        return [*events, PollEvent("error", SOURCE, detail=str(exc))]
    except (MintError, OSError, ValueError) as exc:
        if str(exc) in stuck:  # already said above
            return events
        return [*events, PollEvent("error", SOURCE, detail=str(exc))]
    # Baselined per project filter: pointing the source at another project must not draft
    # everything that project already had approved.
    feed = f"{SOURCE}:{source.project}" if source.project else SOURCE
    if not store.feed_baselined(feed):
        store.baseline_feed(feed, [teaser.id for teaser in teasers])
        events.append(PollEvent("baseline", SOURCE, detail=f"{len(teasers)} teasers already approved"))
        return events
    if not draft:
        return events
    targets = [
        p
        for p in publishers
        if (not source.platforms or p.id in source.platforms)
        and p.media_limits is not None
        and "png" in p.media_limits.formats
    ]
    for teaser in teasers:
        if store.feed_seen(feed, teaser.id):
            continue
        if not teaser.current or teaser.posted:  # superseded, or already posted by someone
            store.mark_feed_seen(feed, teaser.id)
            continue
        if not targets:
            events.append(PollEvent("error", SOURCE, teaser.label, detail="no enabled destination that takes images"))
            continue
        try:
            ids = _draft_teaser(config, store, client, base, targets, teaser)
        except (MintError, HttpError, ReviewError, MediaError, OSError) as exc:
            events.append(PollEvent("error", SOURCE, teaser.label, detail=str(exc)))
            continue
        store.mark_feed_seen(feed, teaser.id)
        labels = ", ".join(f"#{i}" for i in ids) or "drafts already existed"
        events.append(PollEvent("draft", SOURCE, teaser.label, detail=labels, draft_ids=ids))
    return events


def _draft_teaser(
    config: Config, store: Store, http: Http, base: str, targets: list[Publisher], teaser: Teaser
) -> list[int]:
    """Drafts for one teaser, image attached. Everything that can fail is checked before a draft exists."""
    if not teaser.alt:
        raise MintError("the teaser has no alt text")
    image = fetch_image(http, base, teaser)
    folder = config.media_root / SOURCE
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{teaser.id}.png"
    path.write_bytes(image)
    cleaned = clean(path, config.media_root / "clean")
    if _sha256(cleaned.path) != teaser.image_sha256:
        raise MintError(
            "the image carries more than its pixels, so the cleaned copy differs from the one mint "
            "reviewed and mint would not record its post"
        )
    key = KEY_PREFIX + teaser.id
    ids = compose(store, targets, teaser_post(teaser), post_key=key)
    # Also a draft an earlier poll made and then failed to give its image (it died, or the store
    # was busy): that teaser isn't marked seen, so this poll finishes it.
    platforms = {p.id for p in targets}
    for draft in store.list_drafts(post_key=key):
        if draft.id in ids or (
            draft.platform in platforms and draft.status in (PENDING, APPROVED, FAILED) and not store.media(draft.id)
        ):
            attach_media(store, config.media_root, draft.id, path, teaser.alt)
    return ids


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- reporting posts back ----------------------------------------------------------------


def is_teaser(draft: Draft) -> bool:
    return draft.post_key.startswith(KEY_PREFIX)


def record_post(store: Store, draft: Draft, *, at: str | None = None) -> bool:
    """Queue the report for a published teaser draft. True when a new report was queued.

    Nothing is sent here. One report per draft: mint tells two posts apart by their URL and time,
    so a second report with a later-added URL would be recorded as a second post.
    """
    if not is_teaser(draft) or draft.status != PUBLISHED or store.mint_report(draft.id) is not None:
        return False
    images = [m for m in store.media(draft.id) if m["kind"] == "image" and Path(m["path"]).is_file()]
    if not images:
        log.warning("Draft %s has no image left, so its post isn't reported to mint", draft.id)
        return False
    external = (draft.external_id or "").strip()
    return store.add_mint_report(
        draft.id,
        mint_id=draft.post_key[len(KEY_PREFIX) :],
        platform=draft.platform,
        url=external if external.startswith("https://") else None,
        at=at or datetime.now(UTC).isoformat(timespec="seconds"),
        sha256=_sha256(Path(images[0]["path"])),
    )


def send_reports(store: Store, env: Mapping[str, str], http: Http) -> list[Reported]:
    """Send every queued report. One that can't be sent now stays queued."""
    due = store.mint_reports_due()
    if not due:
        return []
    token = (env.get(TOKEN_ENV) or "").strip()
    try:
        base = base_url(env)
        if not token:
            raise MintError(f"Set {TOKEN_ENV} in .env so posts can be reported to mint")
    except MintError as exc:
        return [Reported(row["draft_id"], "waiting", str(exc)) for row in due]
    client = _client(http)
    results: list[Reported] = []
    for index, row in enumerate(due):
        try:
            outcome, detail = _send(client, base, token, row)
        except HttpError as exc:
            detail = _AWAY if exc.status == 0 else str(exc)
            results += [Reported(r["draft_id"], "waiting", detail) for r in due[index:]]
            break
        if outcome != "waiting":
            store.settle_mint_report(
                row["draft_id"], reported=outcome == "sent", refused=outcome == "refused", error=detail
            )
        results.append(Reported(row["draft_id"], outcome, detail))
    return results


def report_note(store: Store, draft: Draft, env: Mapping[str, str], http: Http) -> str:
    """Send what's queued and say, in one line, where this teaser draft's report stands."""
    results: list[Reported] = []
    try:
        results = send_reports(store, env, http)
    except (OSError, ValueError) as exc:  # the confirm already happened; the report waits
        log.warning("Reporting to mint failed: %s", exc)
    row = store.mint_report(draft.id)
    if row is None:
        if (draft.external_id or "").strip():  # the URL is there, so it was the image that was missing
            return "Mint isn't told about this post: the draft no longer has its image."
        return "Mint is told about this post once its URL is recorded (confirm --url)."
    if row["reported_at"]:
        return "Mint has recorded the post."
    if row["refused"]:
        return f"Mint did not record the post: {row['error']}"
    why = next((r.detail for r in results if r.draft_id == draft.id and r.outcome == "waiting"), "")
    if why and why != _AWAY:
        return f"Mint hasn't been told yet ({why}); the next poll tries again."
    return "Mint hasn't been told yet; the next poll tries again."


def _send(http: Http, base: str, token: str, row: dict) -> tuple[str, str]:
    if not _WHERE.fullmatch(row["platform"]):
        return "refused", f"{row['platform']!r} isn't a name mint accepts for where a post went"
    if not _ID.fullmatch(row["mint_id"]):
        return "refused", f"{row['mint_id']!r} isn't a draft id mint accepts"
    url = f"{base}/drafts/{row['mint_id']}/posted"
    payload = {"where": row["platform"], "url": row["url"], "at": row["at"], "sha256": row["sha256"]}
    status, body = send_json(http, url, payload, headers={"Authorization": f"Bearer {token}"})
    if status in (200, 201):
        return "sent", ""
    if status == 401:
        return "waiting", f"mint didn't accept {TOKEN_ENV}"
    if status in (404, 409, 422):
        return "refused", _detail(body) or f"mint answered HTTP {status}"
    raise HttpError("POST", url, status)


def _detail(body: bytes) -> str:
    try:
        detail = json.loads(body.decode("utf-8")).get("detail")
    except (ValueError, AttributeError):
        return ""
    return " ".join(detail.split())[:300] if isinstance(detail, str) else ""
