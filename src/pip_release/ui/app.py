"""Server-rendered review queue for drafts."""

from __future__ import annotations

import hmac
import re
import secrets
from datetime import datetime, timezone
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from pip_release.config import Config, Destination
from pip_release.http import Http, HttpClient, HttpError
from pip_release.models import APPROVED, FAILED, HANDED_OFF, PENDING, PUBLISHING, STATUSES, Draft
from pip_release.models import POST_KINDS, Post
from pip_release.platforms import Publisher, configured_publishers
from pip_release.store import Store, release_key
from pip_release.textutil import plain_to_html
from pip_release.privacy import Gate, GateUnavailable, load_gate, segments
from pip_release.workflow import (
    PrivacyRefused,
    ReviewError,
    LOGGED_KINDS,
    accept_hit,
    attach_media,
    media_problems,
    edit_logged,
    log_broadcast,
    approve,
    compose,
    confirm,
    edit_draft,
    gate_hits,
    publish_draft,
    reject,
)

_ROOT = Path(__file__).resolve().parent
_OPEN = {PENDING, APPROVED, FAILED, HANDED_OFF, PUBLISHING}
_FILTERS = (
    ("open", "Open"),
    ("pending", "Pending"),
    ("approved", "Approved"),
    ("failed", "Failed"),
    ("handed_off", "To post"),
    ("published", "Published"),
    ("all", "All"),
)
_STATUS_LABEL = {
    "pending": "Pending",
    "approved": "Approved",
    "rejected": "Rejected",
    "published": "Published",
    "failed": "Failed",
    "handed_off": "Ready to post",
    "publishing": "Publishing",
}
_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_PROFILE = {
    "twitter": "https://x.com/{handle}",
    "telegram": "https://t.me/{handle}",
    "patreon": "https://www.patreon.com/c/{handle}",
}


def account_links(config: Config, publishers: Mapping[str, Publisher]) -> dict[str, list[dict[str, str]]]:
    """Public pages for the tracked PyPI identity and enabled destinations."""

    profiles = [
        {"label": user, "href": href}
        for user in config.users
        if (href := _pypi_url("user", user))
    ]
    if not profiles:
        profiles = [
            {"label": project, "href": href}
            for project in config.projects
            if (href := _pypi_url("project", project))
        ]
    destinations: list[dict[str, str]] = []
    for platform, destination in config.destinations.items():
        if not destination.enabled:
            continue
        href = _destination_url(platform, destination)
        if href is None:
            continue
        publisher = publishers.get(platform)
        label = publisher.label if publisher else platform
        handle = _handle(destination.username or destination.name)
        if handle:
            label = f"{label} · {handle}"
        destinations.append({"label": label, "href": href})
    return {"profiles": profiles, "destinations": destinations}


def _pypi_url(kind: str, name: str) -> str | None:
    handle = _handle(name)
    if handle is None:
        return None
    return f"https://pypi.org/{kind}/{quote(handle, safe='')}/"


def _destination_url(platform: str, destination: Destination) -> str | None:
    if destination.url:
        safe = _public_url(destination.url)
        if safe:
            return safe
    handle = _handle(destination.username or destination.name)
    pattern = _PROFILE.get(platform)
    if handle and pattern:
        return pattern.format(handle=quote(handle, safe=""))
    return None


def _handle(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip().lstrip("@")
    if _HANDLE.fullmatch(text):
        return text
    return None


def _public_url(url: str) -> str | None:
    text = url.strip()
    if any(char in text for char in " \t\r\n\"'<>"):
        return None
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    return text


def assert_ui_bind(host: str, env: Mapping[str, str]) -> None:
    if host in {"127.0.0.1", "::1", "localhost"}:
        return
    if (env.get("PIP_RELEASE_UI_TOKEN") or "").strip():
        return
    raise PermissionError(
        f"Refusing to listen on {host} without PIP_RELEASE_UI_TOKEN. "
        "The review UI can publish posts."
    )


def create_app(
    config: Config,
    env: Mapping[str, str] | None = None,
    http: Http | None = None,
) -> Starlette:
    settings = dict(env or {})
    secret = (settings.get("PIP_RELEASE_UI_SECRET") or "").strip() or secrets.token_hex(32)
    templates = Jinja2Templates(directory=str(_ROOT / "templates"))
    state = {
        "config": config,
        "env": settings,
        "token": (settings.get("PIP_RELEASE_UI_TOKEN") or "").strip(),
        "http": http,
        "publishers": configured_publishers(config),
        "templates": templates,
    }

    async def login_form(request: Request) -> Response:
        if not state["token"] or request.session.get("reviewer"):
            return RedirectResponse("/", status_code=303)
        return _render(request, "login.html", error="")

    async def login_submit(request: Request) -> Response:
        form = await request.form()
        if not _csrf_ok(request, form.get("csrf")):
            return _expired()
        given = str(form.get("token") or "")
        if not hmac.compare_digest(given, state["token"]):
            return _render(request, "login.html", error="That token does not match.", status_code=401)
        request.session["reviewer"] = True
        return RedirectResponse("/", status_code=303)

    async def queue(request: Request) -> Response:
        denied = _guard(request, state["token"])
        if denied is not None:
            return denied
        status = request.query_params.get("status") or "open"
        if status not in {item[0] for item in _FILTERS}:
            status = "open"
        with _store(config) as store:
            drafts = _select(store, status)
            groups = _groups(drafts)
        return _render(
            request,
            "queue.html",
            status=status,
            filters=[{"id": key, "label": label} for key, label in _FILTERS],
            groups=groups,
            status_label=_STATUS_LABEL,
        )

    async def draft_page(request: Request) -> Response:
        denied = _guard(request, state["token"])
        if denied is not None:
            return denied
        draft_id = int(request.path_params["draft_id"])
        with _store(config) as store:
            draft = store.get(draft_id)
            if draft is None:
                return _render(request, "missing.html", draft_id=draft_id, status_code=404)
            publisher = state["publishers"].get(draft.platform)
            page = _draft_context(config, draft, publisher)
            page.update(_gate_context(store, state, draft))
            page.update(_media_context(store, config, draft, publisher))
            page.update(_schedule_context(store, draft, publisher))
        return _render(request, "draft.html", **page)

    async def save(request: Request) -> Response:
        return await _act(request, state, "save")

    async def approve_one(request: Request) -> Response:
        return await _act(request, state, "approve")

    async def reject_one(request: Request) -> Response:
        return await _act(request, state, "reject")

    async def publish_one(request: Request) -> Response:
        return await _act(request, state, "publish")

    async def compose_form(request: Request) -> Response:
        denied = _guard(request, state["token"])
        if denied is not None:
            return denied
        return _render(request, "compose.html", error="", **_compose_context(state, {}))

    async def compose_submit(request: Request) -> Response:
        denied = _guard(request, state["token"])
        if denied is not None:
            return denied
        form = await request.form()
        if not _csrf_ok(request, form.get("csrf")):
            return _expired()
        chosen = [str(item) for item in form.getlist("platform")]
        enabled = _enabled(state)
        targets = [p for p in enabled if p.id in chosen]
        post = Post(
            kind=str(form.get("kind") or "demo"),
            subject=str(form.get("subject") or "").strip(),
            text=str(form.get("text") or "").strip(),
            link=str(form.get("link") or "").strip(),
            title=str(form.get("title") or "").strip(),
        )
        try:
            if not targets:
                raise ReviewError("Pick at least one destination")
            with _store(config) as store:
                ids = compose(store, targets, post)
        except ReviewError as exc:
            values = {**{k: str(form.get(k) or "") for k in ("kind", "subject", "text", "link", "title")}, "platforms": chosen}
            return _render(request, "compose.html", status_code=400, error=str(exc), **_compose_context(state, values))
        noun = "draft" if len(ids) == 1 else "drafts"
        _flash(request, "ok", f"Created {len(ids)} pending {noun}. Review and approve each one.")
        return RedirectResponse("/?status=pending", status_code=303)

    async def broadcasts_page(request: Request) -> Response:
        denied = _guard(request, state["token"])
        if denied is not None:
            return denied
        with _store(config) as store:
            rows = [dict(row) for row in store.logged_broadcasts()]
        return _render(request, "broadcasts.html", rows=rows, kinds=LOGGED_KINDS, values={}, error="")

    async def broadcast_action(request: Request) -> Response:
        denied = _guard(request, state["token"])
        if denied is not None:
            return denied
        form = await request.form()
        if not _csrf_ok(request, form.get("csrf")):
            return _expired()
        action = request.path_params.get("action", "add")
        target = request.path_params.get("target")
        fields = {k: str(form.get(k) or "").strip() for k in ("platform", "url", "kind", "project", "subject", "text", "at")}
        try:
            with _store(config) as store:
                if action == "add":
                    log_broadcast(store, **{**fields, "kind": fields["kind"] or "other", "at": fields["at"] or None})
                    message = f"Logged the {fields['platform']} post."
                elif action == "edit":
                    edit_logged(store, int(target), **{k: v for k, v in fields.items() if v})
                    message = "Updated."
                elif action == "delete":
                    if not store.delete_logged(int(target)):
                        raise ReviewError(f"No logged broadcast {target}")
                    message = "Deleted."
                else:
                    raise ReviewError(f"Unknown action {action}")
        except ReviewError as exc:
            if action != "add":
                _flash(request, "bad", str(exc))
                return RedirectResponse("/broadcasts", status_code=303)
            with _store(config) as store:
                rows = [dict(row) for row in store.logged_broadcasts()]
            return _render(request, "broadcasts.html", status_code=400, rows=rows, kinds=LOGGED_KINDS, values=fields, error=str(exc))
        _flash(request, "ok", message)
        return RedirectResponse("/broadcasts", status_code=303)

    async def digests_page(request: Request) -> Response:
        denied = _guard(request, state["token"])
        if denied is not None:
            return denied
        with _store(config) as store:
            page = _digests_context(store, config)
        return _render(request, "digests.html", **page)

    async def digest_action(request: Request) -> Response:
        denied = _guard(request, state["token"])
        if denied is not None:
            return denied
        form = await request.form()
        if not _csrf_ok(request, form.get("csrf")):
            return _expired()
        action = request.path_params["action"]
        target = int(request.path_params["target"])
        try:
            if config.digest is None:
                raise ReviewError("Digest mode is off")
            message = _digest_perform(state, config, action, target, form)
        except (ReviewError, ValueError, GateUnavailable, OSError) as exc:
            _flash(request, "bad", str(exc))
            return RedirectResponse("/digests", status_code=303)
        _flash(request, "ok", message)
        return RedirectResponse("/digests", status_code=303)

    async def media_file(request: Request) -> Response:
        denied = _guard(request, state["token"])
        if denied is not None:
            return denied
        with _store(config) as store:
            item = store.media_item(int(request.path_params["media_id"]))
        root = (config.media_root / "clean").resolve()
        path = Path(item["path"]).resolve() if item else None
        if path is None or root not in path.parents or not path.is_file():
            return Response("Not found", status_code=404)
        return FileResponse(path, headers={"Cache-Control": "private, max-age=300"})

    async def media_add(request: Request) -> Response:
        denied = _guard(request, state["token"])
        if denied is not None:
            return denied
        form = await request.form(max_files=1)
        if not _csrf_ok(request, form.get("csrf")):
            return _expired()
        draft_id = int(request.path_params["draft_id"])
        try:
            source, name = await _media_source(config, form)
            with _store(config) as store:
                ids = attach_media(store, config.media_root, draft_id, source, str(form.get("alt") or ""),
                                   all_drafts=form.get("all_drafts") == "1", name=name)
                item = store.media_item(ids[0])
        except (ReviewError, OSError) as exc:
            _flash(request, "bad", str(exc))
            return RedirectResponse(f"/drafts/{draft_id}", status_code=303)
        where = f" to {len(ids)} drafts" if len(ids) > 1 else ""
        _flash(request, "ok", f"Attached {name}{where}. Metadata removed: {item['removed']}.")
        return RedirectResponse(f"/drafts/{draft_id}", status_code=303)

    async def media_edit(request: Request) -> Response:
        denied = _guard(request, state["token"])
        if denied is not None:
            return denied
        form = await request.form()
        if not _csrf_ok(request, form.get("csrf")):
            return _expired()
        media_id = int(request.path_params["media_id"])
        with _store(config) as store:
            item = store.media_item(media_id)
            if item is None:
                return Response("Not found", status_code=404)
            draft = store.get(item["draft_id"])
            if draft is None or draft.status not in (PENDING, APPROVED, FAILED):
                _flash(request, "bad", "This draft's media can't change now.")
            elif request.path_params["action"] == "remove":
                store.remove_media(media_id)
                _flash(request, "ok", f"Removed {item['name']}.")
            else:
                store.set_alt(media_id, str(form.get("alt") or "").strip())
                if draft.status == APPROVED:
                    store.transition(draft.id, (APPROVED,), PENDING)
                _flash(request, "ok", "Alt text saved.")
        return RedirectResponse(f"/drafts/{item['draft_id']}", status_code=303)

    async def confirm_one(request: Request) -> Response:
        return await _act(request, state, "confirm")

    async def accept_one(request: Request) -> Response:
        return await _act(request, state, "accept")

    async def regenerate_one(request: Request) -> Response:
        return await _act(request, state, "regenerate")

    async def schedule_one(request: Request) -> Response:
        return await _act(request, state, "schedule")

    async def approve_release(request: Request) -> Response:
        denied = _guard(request, state["token"])
        if denied is not None:
            return denied
        form = await request.form()
        if not _csrf_ok(request, form.get("csrf")):
            return _expired()
        post_key = str(form.get("post_key") or "")
        if not post_key and form.get("project") and form.get("version"):
            # A queue page rendered before post keys existed.
            post_key = release_key(str(form.get("project")), str(form.get("version")))
        refused: list[str] = []
        count = 0
        try:
            gate = _gate(state)
            with _store(config) as store:
                pending = [
                    draft
                    for draft in (store.list_drafts(post_key=post_key) if post_key else [])
                    if draft.status == PENDING
                ]
                for draft in pending:
                    publisher = _publisher(state["publishers"], draft.platform)
                    try:
                        approve(store, publisher, draft.id, None, gate=gate)
                    except PrivacyRefused:
                        refused.append(draft.platform)
                        continue
                    count += 1
        except (ReviewError, ValueError, GateUnavailable) as exc:
            _flash(request, "bad", str(exc))
            return RedirectResponse(f"/?status=open", status_code=303)
        noun = "draft" if count == 1 else "drafts"
        label = pending[0].label if pending else post_key
        if refused:
            _flash(
                request,
                "bad",
                f"Approved {count} {noun} for {label}. The privacy gate refused "
                f"{', '.join(refused)}: open each draft to see why.",
            )
            return RedirectResponse("/?status=pending", status_code=303)
        _flash(request, "ok", f"Approved {count} {noun} for {label}.")
        return RedirectResponse("/?status=approved", status_code=303)

    routes = [
        Route("/login", login_form, methods=["GET"]),
        Route("/login", login_submit, methods=["POST"]),
        Route("/", queue, methods=["GET"]),
        Route("/broadcasts", broadcasts_page, methods=["GET"]),
        Route("/broadcasts", broadcast_action, methods=["POST"]),
        Route("/broadcasts/{action:str}/{target:int}", broadcast_action, methods=["POST"]),
        Route("/digests", digests_page, methods=["GET"]),
        Route("/digests/{action:str}/{target:int}", digest_action, methods=["POST"]),
        Route("/compose", compose_form, methods=["GET"]),
        Route("/compose", compose_submit, methods=["POST"]),
        Route("/drafts/{draft_id:int}", draft_page, methods=["GET"]),
        Route("/drafts/{draft_id:int}/save", save, methods=["POST"]),
        Route("/drafts/{draft_id:int}/approve", approve_one, methods=["POST"]),
        Route("/drafts/{draft_id:int}/reject", reject_one, methods=["POST"]),
        Route("/drafts/{draft_id:int}/publish", publish_one, methods=["POST"]),
        Route("/drafts/{draft_id:int}/confirm", confirm_one, methods=["POST"]),
        Route("/drafts/{draft_id:int}/accept", accept_one, methods=["POST"]),
        Route("/drafts/{draft_id:int}/regenerate", regenerate_one, methods=["POST"]),
        Route("/drafts/{draft_id:int}/schedule", schedule_one, methods=["POST"]),
        Route("/drafts/{draft_id:int}/media", media_add, methods=["POST"]),
        Route("/media/{media_id:int}", media_file, methods=["GET"]),
        Route("/media/{media_id:int}/{action:str}", media_edit, methods=["POST"]),
        Route("/releases/approve", approve_release, methods=["POST"]),
        Mount("/static", app=StaticFiles(directory=str(_ROOT / "static")), name="static"),
    ]
    app = Starlette(routes=routes)
    app.state.templates = templates
    app.state.accounts = account_links(config, state["publishers"])
    app.state.locked = bool(state["token"])
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="pip_release_review",
        same_site="lax",
        https_only=False,
        max_age=14 * 24 * 60 * 60,
    )
    return app


async def _act(request: Request, state: dict, action: str) -> Response:
    denied = _guard(request, state["token"])
    if denied is not None:
        return denied
    form = await request.form()
    if not _csrf_ok(request, form.get("csrf")):
        return _expired()
    draft_id = int(request.path_params["draft_id"])
    config: Config = state["config"]
    try:
        with _store(config) as store:
            draft = store.get(draft_id)
            if draft is None:
                raise ReviewError(f"No draft {draft_id}")
            publisher = _publisher(state["publishers"], draft.platform)
            message = _perform(store, publisher, draft, form, action, state, config)
    except PrivacyRefused:
        _flash(request, "bad", "The privacy gate refused this draft. Each hit is marked below.")
        return RedirectResponse(f"/drafts/{draft_id}", status_code=303)
    except (ReviewError, ValueError, HttpError, OSError, GateUnavailable) as exc:
        _flash(request, "bad", str(exc))
        return RedirectResponse(f"/drafts/{draft_id}", status_code=303)
    _flash(request, "ok", message)
    return RedirectResponse(f"/drafts/{draft_id}", status_code=303)


def _perform(store, publisher: Publisher, draft: Draft, form, action: str, state: dict, config: Config) -> str:
    note = str(form.get("note") or "").strip() or None
    if action == "reject":
        reject(store, draft.id, note)
        return f"Rejected {draft.platform} for {draft.label}."
    if action == "confirm":
        url = str(form.get("url") or "").strip() or None
        confirmed = confirm(store, draft.id, url)
        where = " with its post URL" if confirmed.external_id else ""
        return f"Marked {draft.label} published on {publisher.label}{where}."
    if action == "schedule":
        from pip_release.schedule import iso, parse

        if draft.status != APPROVED:
            raise ReviewError("Approve the draft before scheduling it")
        choice = str(form.get("when") or "").strip()
        custom = str(form.get("at") or "").strip()
        if choice == "clear":
            store.set_publish_at(draft.id, None)
            return "Unscheduled. It goes out when you press Publish."
        try:
            when = parse(custom or choice)
        except ValueError as exc:
            raise ReviewError("Pick a slot or enter a time like 2026-12-01T08:00 (UTC)") from exc
        if when <= datetime.now(timezone.utc):
            raise ReviewError("That time has passed. Use Publish to post now.")
        store.set_publish_at(draft.id, iso(when))
        return f"Scheduled for {_when(iso(when))}."
    if action == "regenerate":
        from pip_release.digest import regenerate

        try:
            gate = _gate(state)
        except GateUnavailable:
            gate = None
        backend, warning = _backend(state)
        try:
            message = regenerate(store, config.digest, publisher, draft.id, backend, gate, force=form.get("force") == "1")
        except ValueError as exc:
            raise ReviewError(str(exc)) from exc
        return f"{message} {warning}".strip()
    if action == "accept":
        key = str(form.get("key") or "")
        accept_hit(store, _gate(state), draft.id, key, note)
        return "Accepted that hit for this draft."
    if action == "publish":
        client = state["http"] or HttpClient()
        result = publish_draft(store, publisher, draft, state["env"], client, gate=_gate(state))
        if result.handed_off:
            return f"{draft.label} is ready to post on {publisher.label}."
        return result.detail
    title = str(form.get("title") if form.get("title") is not None else draft.title)
    body = str(form.get("body") if form.get("body") is not None else draft.body)
    reply = str(form.get("reply")).strip() if form.get("reply") is not None else draft.reply
    if title != draft.title or body != draft.body or reply != draft.reply:
        draft = edit_draft(store, publisher, draft.id, title, body, reply)
    if action == "save":
        return "Saved. This draft is pending approval."
    approved = approve(store, publisher, draft.id, note, gate=_gate(state), schedules=_schedules(config))
    if approved.publish_at:
        return f"Approved {draft.platform} for {draft.label}; scheduled for {_when(approved.publish_at)}."
    return f"Approved {draft.platform} for {draft.label}."


def _draft_context(config: Config, draft: Draft, publisher: Publisher | None) -> dict:
    limit = getattr(publisher, "limit", 10000) if publisher else 10000
    destination = config.destinations.get(draft.platform)
    handoff = None
    if draft.status == HANDED_OFF and publisher is not None:
        link = publisher.handoff_url(draft)
        if link:
            handoff = {"kind": "intent", "url": link, "text": draft.body}
        else:
            where = destination.url if destination and destination.url else "https://www.patreon.com"
            handoff = {"kind": "paste", "url": where, "title": draft.title, "html": plain_to_html(draft.body)}
    return {
        "draft": draft,
        "label": publisher.label if publisher else draft.platform,
        "limit": limit,
        "weighted": draft.platform == "twitter",
        "show_title": draft.platform != "twitter",
        "status_label": _STATUS_LABEL.get(draft.status, draft.status),
        "can_edit": draft.status in {PENDING, APPROVED, FAILED},
        "can_approve": draft.status == PENDING,
        "can_publish": draft.status in {APPROVED, FAILED, PUBLISHING},
        "can_reject": draft.status not in {"rejected", "published"},
        "can_confirm": draft.status == HANDED_OFF,
        "can_add_url": draft.status == "published" and not draft.external_id and bool(publisher and publisher.manual),
        "audience": publisher.audience if publisher else None,
        "reply_url": publisher.reply_url(draft) if publisher and hasattr(publisher, "reply_url") else None,
        "handoff": handoff,
        "handoff_manual": bool(publisher and publisher.manual),
    }


def _release_view(store: Store, rows) -> list[dict]:
    from pip_release.digest import highlight, notable

    suggested = {row["id"] for row, _line, flag in notable(store, [r for r in rows if not r["excluded"]]) if flag}
    return [
        {
            "id": row["id"], "project": row["project"], "version": row["version"], "source": row["source"],
            "released_at": row["released_at"][:10], "excluded": bool(row["excluded"]), "url": row["url"],
            "suggested": row["id"] in suggested, "highlight": highlight(row["notes"], row["summary"]),
            "promoted": bool(store.list_drafts(post_key=f"headline:{row['project']}=={row['version']}")),
        }
        for row in rows
    ]


def _digests_context(store: Store, config: Config) -> dict:
    from pip_release.digest import current_window, period_label, window_of

    settings = config.digest
    if settings is None:
        return {"enabled": False}
    now = datetime.now(timezone.utc)
    window = current_window(settings, now)
    past = []
    for row in store.digests():
        drafts = store.list_drafts(post_key=f"digest:{row['period_from']}")
        past.append({
            "id": row["id"], "label": period_label(window_of(row)), "site_path": row["site_path"],
            "error": row["error"], "drafts": drafts, "releases": _release_view(store, store.releases(digest_id=row["id"])),
        })
    return {
        "enabled": True,
        "open": {"label": period_label(window), "closes": window.end.isoformat(),
                 "releases": _release_view(store, store.releases(open_only=True))},
        "past": past,
        "site_dir": str(settings.site_dir) if settings.site_dir else "",
    }


def _digest_perform(state: dict, config: Config, action: str, target: int, form) -> str:
    from pip_release.digest import build_digest, promote

    settings = config.digest
    with _store(config) as store:
        if action in ("exclude", "include"):
            row = store.conn.execute("SELECT * FROM releases WHERE id = ?", (target,)).fetchone()
            if row is None:
                raise ReviewError(f"No release {target}")
            store.set_excluded(target, action == "exclude")
            hint = " Rebuild the digest to refresh its drafts and page." if row["digest_id"] else ""
            return f"{action.capitalize()}d {row['project']} {row['version']}.{hint}"
        if action == "rebuild":
            try:
                gate = _gate(state)
            except GateUnavailable:
                gate = None
            backend, warning = _backend(state)
            ids, status = build_digest(store, settings, _enabled(state), target, gate, backend)
            extra = f" Note: {warning}." if warning else ""
            return f"Rebuilt: {len(ids)} new drafts; unedited drafts refreshed; page {status}.{extra}"
        if action == "promote":
            row = store.conn.execute("SELECT * FROM releases WHERE id = ?", (target,)).fetchone()
            if row is None:
                raise ReviewError(f"No release {target}")
            site_page = form.get("site_page") == "1"
            backend, _warning = _backend(state)
            try:
                gate = _gate(state)
            except GateUnavailable:
                if site_page:
                    raise
                gate = None
            ids, status = promote(store, settings, _enabled(state), row, site_page=site_page, gate=gate, backend=backend)
            extra = f" Site page: {status}." if status else ""
            return f"Promoted {row['project']} {row['version']}: {len(ids)} headline drafts.{extra}"
    raise ReviewError(f"Unknown action {action}")


MAX_UPLOAD = 600 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _library(config: Config) -> list[str]:
    """Files in the media folder that can be picked (not the cleaned or uploaded copies)."""
    from pip_release.media import GIF_EXT, IMAGE_EXT, VIDEO_EXT

    root = config.media_root
    if not root.is_dir():
        return []
    allowed = set(IMAGE_EXT) | GIF_EXT | VIDEO_EXT
    return sorted(p.name for p in root.iterdir() if p.is_file() and p.suffix.lower() in allowed)


async def _media_source(config: Config, form) -> tuple[Path, str]:
    upload = form.get("file")
    root = config.media_root
    if upload is not None and getattr(upload, "filename", ""):
        name = _SAFE_NAME.sub("-", Path(upload.filename).name).strip("-") or "upload"
        target_dir = root / "uploads"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{secrets.token_hex(4)}-{name}"
        written = 0
        with target.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD:
                    break
                handle.write(chunk)
        if written > MAX_UPLOAD:
            target.unlink(missing_ok=True)
            raise ReviewError(f"Uploads are limited to {MAX_UPLOAD // (1024 * 1024)} MB")
        return target, name
    picked = str(form.get("library") or "")
    if picked:
        path = (root / picked).resolve()
        if path.parent != root.resolve() or not path.is_file():
            raise ReviewError("Pick a file from the media folder")
        return path, path.name
    raise ReviewError("Upload a file or pick one from the media folder")


def _media_context(store: Store, config: Config, draft: Draft, publisher: Publisher | None) -> dict:
    media = store.media(draft.id)
    problems = media_problems(store, publisher, draft) if publisher and media else []
    limits = publisher.media_limits if publisher else None
    return {
        "media": media,
        "media_problems": problems,
        "media_supported": limits is not None,
        "library": _library(config),
        "media_dir": str(config.media_root),
        "siblings": len(store.list_drafts(post_key=draft.post_key)) > 1,
    }


def _schedules(config: Config) -> dict:
    return {name: d.schedule for name, d in config.destinations.items() if d.schedule is not None}


def _when(value: str | None) -> str:
    if not value:
        return ""
    from pip_release.schedule import parse

    stamp = parse(value)
    return f"{stamp:%a %d %b %H:%M} UTC"


def _schedule_context(store: Store, draft: Draft, publisher: Publisher | None) -> dict:
    from pip_release.schedule import free_slots, iso

    choices = []
    if draft.status == APPROVED and publisher is not None and publisher.schedule is not None:
        for when in free_slots(store, draft.platform, publisher.schedule, datetime.now(timezone.utc), ignore=draft.id):
            choices.append({"value": iso(when), "label": _when(iso(when))})
    return {"publish_at_label": _when(draft.publish_at), "slot_choices": choices,
            "has_schedule": bool(publisher and publisher.schedule)}


def _backend(state: dict):
    from pip_release.drafting import configured_backend

    return configured_backend(state["config"].drafting, state["env"], state["http"] or HttpClient())


def _enabled(state: dict) -> list[Publisher]:
    config: Config = state["config"]
    return [
        publisher
        for name, destination in config.destinations.items()
        if destination.enabled and (publisher := state["publishers"].get(name)) is not None
    ]


def _compose_context(state: dict, values: dict) -> dict:
    enabled = _enabled(state)
    chosen = values.get("platforms") or [p.id for p in enabled]
    return {
        "kinds": POST_KINDS,
        "destinations": [{"id": p.id, "label": p.label, "checked": p.id in chosen} for p in enabled],
        "values": {"kind": "demo", **values},
    }


def _gate(state: dict) -> Gate:
    """Loaded per action, so an edited deny-list applies without a restart."""
    return load_gate(state["config"].privacy, state["env"])


def _gate_context(store: Store, state: dict, draft: Draft) -> dict:
    try:
        gate = _gate(state)
    except GateUnavailable as exc:
        return {"gate_error": str(exc), "gate_warnings": [], "hits": [], "marked": None, "accepted": {}}
    hits = gate_hits(store, gate, draft) if draft.status not in {"rejected", "published"} else []
    body_hits = [hit for hit in hits if hit.field == "body"]
    return {
        "gate_error": None,
        "gate_warnings": gate.warnings,
        "hits": hits,
        "marked": segments(draft.body, body_hits) if body_hits else None,
        "accepted": store.accepted(draft.id),
    }


def _select(store: Store, status: str) -> list[Draft]:
    if status == "all":
        return store.list_drafts()
    if status == "open":
        return [draft for draft in store.list_drafts() if draft.status in _OPEN]
    if status not in STATUSES:
        return [draft for draft in store.list_drafts() if draft.status in _OPEN]
    return store.list_drafts(status=status)


def _groups(drafts: list[Draft]) -> list[dict]:
    groups: list[dict] = []
    index: dict[str, dict] = {}
    for draft in drafts:
        key = draft.post_key
        group = index.get(key)
        if group is None:
            group = {
                "post_key": key,
                "kind": draft.kind,
                "label": draft.label,
                "project": draft.project,
                "version": draft.version,
                "link": draft.link,
                "drafts": [],
                "pending": 0,
            }
            index[key] = group
            groups.append(group)
        group["drafts"].append(draft)
        if draft.status == PENDING:
            group["pending"] += 1
    return groups


def _publisher(publishers: Mapping[str, Publisher], platform: str) -> Publisher:
    try:
        return publishers[platform]
    except KeyError as exc:
        raise ReviewError(f"No publisher named {platform}") from exc


class _store:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store: Store | None = None

    def __enter__(self) -> Store:
        self.store = Store(self.config.store_path)
        return self.store

    def __exit__(self, *exc: object) -> None:
        if self.store is not None:
            self.store.close()


def _guard(request: Request, token: str) -> Response | None:
    if token and not request.session.get("reviewer"):
        return RedirectResponse("/login", status_code=303)
    return None


def _csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def _csrf_ok(request: Request, given: object) -> bool:
    expected = request.session.get("csrf")
    if not isinstance(expected, str) or not isinstance(given, str):
        return False
    return hmac.compare_digest(expected, given)


def _flash(request: Request, kind: str, text: str) -> None:
    request.session["flash"] = {"kind": kind, "text": text}


def _render(request: Request, name: str, status_code: int = 200, **context: object) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    context["flash"] = request.session.pop("flash", None)
    context["csrf"] = _csrf(request)
    context.setdefault("filters", None)
    context.setdefault("status", "")
    # Before sign-in the page shows nothing about the queue: no sections, packages or accounts.
    signed_in = not getattr(request.app.state, "locked", False) or bool(request.session.get("reviewer"))
    context["signed_in"] = signed_in
    context.setdefault("accounts", getattr(request.app.state, "accounts", None) if signed_in else None)
    theme = request.cookies.get("theme")
    context["theme"] = theme if theme in ("light", "dark") else None
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def _expired() -> HTMLResponse:
    return HTMLResponse("This form expired. Reload the page and try again.", status_code=400)
