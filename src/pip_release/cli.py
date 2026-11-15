"""Command line for polling, review, and publish."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from pip_release.config import (
    Config,
    ConfigError,
    Destination,
    load_config,
    load_env_file,
    resolve_config_path,
)
from pip_release.http import HttpClient, HttpError
from pip_release.models import APPROVED, PENDING, POST_KINDS, Draft, Post
from pip_release.privacy import Gate, GateUnavailable, load_gate
from pip_release.platforms import Publisher, configured_publishers, load_publishers
from pip_release.pypi import ProfileUnavailable, discover_projects
from pip_release.store import Store
from pip_release.textutil import plain_to_html
from pip_release.workflow import (
    PollEvent,
    PrivacyRefused,
    ReviewError,
    accept_hit,
    gate_hits,
    approve,
    compose,
    confirm,
    edit_draft,
    desktop_notice,
    REMINDER_KEY,
    STALE_NOTE,
    notify_review,
    poll,
    reminder_due,
    publish_draft,
    rebaseline,
    reject,
)

log = logging.getLogger("pip_release")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "init":
            return cmd_init(args.example, args.force)
        if args.command == "healthcheck":
            return cmd_healthcheck()
        load_env_file(Path(args.env))
        config = load_config(resolve_config_path(args.config))
        if args.store:
            config.store_path = Path(args.store)
        if args.command == "discover":
            if args.check:
                return cmd_discover_check(config, HttpClient(), os.environ)
            return cmd_discover(config)
        if args.command == "ui":
            return cmd_ui(config, args.host, args.port)
        store = Store(config.store_path)
        try:
            return _dispatch(args, config, store)
        finally:
            store.close()
    except (ConfigError, ReviewError, ProfileUnavailable, GateUnavailable) as exc:
        print(exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _dispatch(args: argparse.Namespace, config: Config, store: Store) -> int:
    publishers = _selected(config)
    by_id = configured_publishers(config)
    http = HttpClient()
    if args.command == "platforms":
        return cmd_platforms(config, by_id, os.environ)
    if args.command == "poll":
        return cmd_poll(config, store, http, publishers)
    if args.command == "baseline":
        return cmd_baseline(config, store, http, args.projects)
    if args.command == "reset":
        if not args.reject_pending:
            raise ReviewError("Nothing to reset. Pass --reject-pending.")
        return cmd_reset(store, args.before, args.yes, args.note)
    if args.command == "watch":
        return cmd_watch(config, store, http, publishers)
    if args.command == "drafts":
        return cmd_drafts(store, args.status)
    if args.command == "show":
        return cmd_show(store, args.id)
    if args.command == "edit":
        return cmd_edit(store, by_id, args.id)
    if args.command == "approve":
        return cmd_approve(store, by_id, _ids(args, store), args.note, _gate(config))
    if args.command == "reject":
        return cmd_reject(store, _ids(args, store), args.note)
    if args.command == "publish":
        return cmd_publish(config, store, by_id, http, _ids(args, store), args.dry_run, _gate(config))
    if args.command == "collect":
        return cmd_collect(config, store, http, args.since)
    if args.command == "digest":
        return cmd_digest(config, store, publishers, args)
    if args.command == "promote":
        return cmd_promote(config, store, publishers, args)
    if args.command == "compose":
        return cmd_compose(store, publishers, args)
    if args.command == "check":
        return cmd_check(store, _gate(config), args.ids)
    if args.command == "accept":
        accept_hit(store, _gate(config), args.id, args.key, args.note)
        print(f"Accepted {args.key!r} for draft {args.id}.")
        return 0
    if args.command == "confirm":
        return cmd_confirm(store, args.id, args.url)
    raise ReviewError(f"Unknown command {args.command}")


def cmd_ui(config: Config, host: str, port: int) -> int:
    from pip_release.ui.app import assert_ui_bind, create_app

    try:
        assert_ui_bind(host, os.environ)
    except PermissionError as exc:
        raise ConfigError(str(exc)) from exc
    import uvicorn

    print(f"Review UI at http://{host}:{port}/")
    uvicorn.run(create_app(config, os.environ), host=host, port=port, log_level="info")
    return 0


def cmd_init(example: str | None, force: bool) -> int:
    source = Path("examples/dragonhound.yaml" if example == "dragonhound" else "config.example.yaml")
    if not source.is_file():
        raise ConfigError(f"Missing {source}. Run init from the repository root.")
    _copy(source, Path("config.yaml"), force)
    env_example = Path(".env.example")
    if env_example.is_file():
        _copy(env_example, Path(".env"), force)
    print(f"Wrote config.yaml from {source}")
    return 0


def cmd_discover(config: Config) -> int:
    if not config.users:
        raise ConfigError("Set pypi.user before running discover.")
    http = HttpClient()
    failed = False
    for user in config.users:
        print(f"# {user}")
        try:
            for name in discover_projects(http, user):
                print(f"  - {name}")
        except (ProfileUnavailable, HttpError) as exc:
            print(f"  # {exc}", file=sys.stderr)
            failed = True
    if failed:
        print("List projects explicitly under pypi.projects.", file=sys.stderr)
        return 1
    return 0


def cmd_discover_check(config: Config, http, env: Mapping[str, str]) -> int:
    from pip_release.discover import check_projects

    drift = check_projects(config, http, env)
    for note in drift.notes:
        print(f"# {note}", file=sys.stderr)
    for name in drift.missing:
        print(f"missing on PyPI: {name} (configured, but PyPI has no such project)")
    if drift.unconfigured:
        print("not tracked (add to pypi.projects; the first poll baselines them, nothing old is drafted):")
        for name, where in sorted(drift.unconfigured.items()):
            print(f"  - {name}    # {where}")
    if drift.clean:
        print(f"OK: {len(config.projects)} configured projects, no drift.")
        return 0
    return 1


def cmd_collect(config: Config, store: Store, http, since: str | None) -> int:
    from datetime import date as _date

    from pip_release.metrics import Metrics, collect, metrics_path

    start = None
    if since:
        try:
            start = _date.fromisoformat(since)
        except ValueError as exc:
            raise ReviewError("--since must be a date like 2026-11-01") from exc
    metrics = Metrics(metrics_path(config))
    try:
        report = collect(config, store, metrics, http, os.environ, since=start)
    finally:
        metrics.close()
    for line in report.lines:
        print(line)
    for line in report.skipped:
        print(f"skipped {line}", file=sys.stderr)
    for line in report.errors:
        print(f"error {line}", file=sys.stderr)
    return 1 if report.errors else 0


COLLECT_KEY = "collected_at"
COLLECT_EVERY = 24 * 3600


def _collect_daily(config: Config, store: Store, http: HttpClient) -> None:
    """From the watcher. GitHub keeps traffic for 14 days, so a daily run loses nothing."""
    last = store.get_meta(COLLECT_KEY)
    now = time.time()
    if last and now - float(last) < COLLECT_EVERY:
        return
    from pip_release.metrics import Metrics, collect, metrics_path

    metrics = Metrics(metrics_path(config))
    try:
        report = collect(config, store, metrics, http, os.environ)
    finally:
        metrics.close()
    store.set_meta(COLLECT_KEY, str(now))
    for line in report.errors:
        log.warning("collect: %s", line)


DISCOVER_KEY = "discover_checked_at"
DISCOVER_EVERY = 24 * 3600


def _discover_daily(config: Config, store: Store, http: HttpClient) -> None:
    """From the watcher: check for drift once a day and raise a desktop notice when found."""
    if not (config.discover_owner or config.users):
        return
    last = store.get_meta(DISCOVER_KEY)
    now = time.time()
    if last and now - float(last) < DISCOVER_EVERY:
        return
    from pip_release.discover import check_projects

    try:
        drift = check_projects(config, http, os.environ)
    except (HttpError, OSError, ValueError) as exc:
        log.warning("discover check failed: %s", exc)
        return
    store.set_meta(DISCOVER_KEY, str(now))
    if drift.clean:
        return
    names = [*drift.unconfigured, *(f"{n} (missing on PyPI)" for n in drift.missing)]
    log.warning("project drift: %s", ", ".join(names))
    _notify_text("Tracked projects have drifted", "\n".join(names[:6]) + "\nRun: pip-release discover --check")


def cmd_platforms(
    config: Config, publishers: Mapping[str, Publisher], env: Mapping[str, str]
) -> int:
    if not config.destinations:
        print("No destinations in the config.")
        return 0
    for name, destination in config.destinations.items():
        publisher = publishers.get(name)
        if publisher is None:
            print(f"{name:<12} unknown platform")
            continue
        state = "enabled" if destination.enabled else "disabled"
        who = destination.username or destination.name or ""
        missing = publisher.missing_secrets(env) if destination.enabled else []
        extra = f"  missing {', '.join(missing)}" if missing else ""
        account = f"  {who}" if who else ""
        how = "  manual" if publisher.manual and destination.enabled else ""
        print(f"{publisher.label:<16} {state:<10}{account}{how}{extra}")
    return 0


def cmd_poll(config: Config, store: Store, http: HttpClient, publishers: list[Publisher]) -> int:
    events = poll(config, store, http, publishers)
    new_ids: list[int] = []
    errors = 0
    for event in events:
        print(_format_event(event))
        new_ids.extend(event.draft_ids)
        if event.kind == "error":
            errors += 1
    drafts = [draft for draft_id in new_ids if (draft := store.get(draft_id))]
    review = (os.environ.get("PIP_RELEASE_UI_URL") or "").strip()
    if drafts and review:
        print(f"Review {review}")
    if drafts:
        try:
            notify_review(http, os.environ, drafts)
        except (HttpError, OSError, ValueError) as exc:
            print(f"Review notification failed: {exc}", file=sys.stderr)
        _desktop_notify(drafts)
        _mark_reminder(store)
    else:
        _remind_pending(store, http)
    _heartbeat(config)
    return 1 if errors else 0


def cmd_baseline(
    config: Config, store: Store, http: HttpClient, projects: list[str] | None
) -> int:
    events = rebaseline(config, store, http, projects or None)
    for event in events:
        print(_format_event(event))
    return 1 if any(event.kind == "error" for event in events) else 0


def cmd_reset(store: Store, before: str | None, yes: bool, note: str) -> int:
    cutoff = parse_cutoff(before) if before else None
    drafts = store.pending_before(cutoff)
    scope = f" created before {cutoff}" if cutoff else ""
    if not drafts:
        print(f"No pending drafts{scope}.")
        return 0
    print(f"{len(drafts)} pending drafts{scope}:")
    for project, count in sorted(Counter(draft.label for draft in drafts).items()):
        print(f"  {project:<24} {count}")
    created = sorted(draft.created_at for draft in drafts)
    print(f"  oldest {created[0]}, newest {created[-1]}")
    if not yes and not _confirm(f"Reject {len(drafts)} drafts with note {note!r}? [y/N] "):
        print("Nothing changed.")
        return 1
    changed = store.reject_pending([draft.id for draft in drafts], note)
    print(f"Rejected {changed}.")
    return 0


def parse_cutoff(value: str) -> str:
    """An ISO date or timestamp as a string that compares correctly with created_at (UTC)."""
    try:
        if len(value) == 10:
            return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewError(f"--before must be an ISO date or timestamp, not {value!r}") from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        raise ReviewError("No terminal to confirm on. Pass --yes.")
    return input(prompt).strip().lower() in ("y", "yes")


def _remind_pending(store: Store, http: HttpClient) -> None:
    pending = store.list_drafts(status=PENDING)
    if not pending:
        return
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if not reminder_due(store.get_meta(REMINDER_KEY), now):
        return
    review = (os.environ.get("PIP_RELEASE_UI_URL") or "").strip()
    print(f"Reminder: {len(pending)} draft(s) still waiting" + (f" — {review}" if review else ""))
    try:
        notify_review(http, os.environ, pending, event="drafts.waiting")
    except (HttpError, OSError, ValueError) as exc:
        print(f"Review reminder failed: {exc}", file=sys.stderr)
    _desktop_notify(pending, still_waiting=True)
    _mark_reminder(store, now)


def _mark_reminder(store: Store, now: datetime | None = None) -> None:
    stamp = now or datetime.now(timezone.utc).replace(microsecond=0)
    store.set_meta(REMINDER_KEY, stamp.isoformat())


def _desktop_notify(drafts: list[Draft], *, still_waiting: bool = False) -> None:
    review = (os.environ.get("PIP_RELEASE_UI_URL") or "").strip()
    notice = desktop_notice(drafts, review, still_waiting=still_waiting)
    if notice is not None:
        _notify_text(*notice)


def _notify_text(summary: str, body: str) -> None:
    flag = (os.environ.get("PIP_RELEASE_DESKTOP_NOTIFY") or "1").strip().lower()
    binary = shutil.which("notify-send")
    if flag in {"0", "false", "no", "off"} or not binary:
        return
    try:
        subprocess.run(
            [
                binary,
                "--app-name=pip-release",
                "--icon=software-update-available",
                "--urgency=normal",
                summary,
                body,
            ],
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Desktop notification failed: {exc}", file=sys.stderr)


def cmd_watch(config: Config, store: Store, http: HttpClient, publishers: list[Publisher]) -> int:
    while True:
        try:
            cmd_poll(config, store, http, publishers)
            _discover_daily(config, store, http)
            _collect_daily(config, store, http)
        except (ConfigError, ReviewError, HttpError, OSError, ValueError) as exc:
            log.error("%s", exc)
            _heartbeat(config)
        time.sleep(config.interval_seconds)


def cmd_drafts(store: Store, status: str | None) -> int:
    drafts = store.list_drafts(status=status)
    if not drafts:
        print("No drafts.")
        return 0
    print(f"{'ID':<6}{'KIND':<9}{'CONTENT':<36}{'PLATFORM':<12}{'STATUS':<14}EDITED")
    for draft in drafts:
        edited = "yes" if draft.edited else "no"
        label = draft.label if len(draft.label) <= 34 else draft.label[:31] + "..."
        print(
            f"{draft.id:<6}{draft.kind:<9}{label:<36}"
            f"{draft.platform:<12}{draft.status:<14}{edited}"
        )
    return 0


def cmd_show(store: Store, draft_id: int) -> int:
    draft = _one(store, draft_id)
    _print_draft(draft)
    return 0


def cmd_edit(store: Store, publishers: Mapping[str, Publisher], draft_id: int) -> int:
    draft = _one(store, draft_id)
    publisher = _publisher(publishers, draft.platform)
    original = f"title: {draft.title}\n\n{draft.body}\n"
    path = _temp_path(draft_id)
    path.write_text(original, encoding="utf-8")
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    completed = subprocess.run([editor, str(path)], check=False)
    if completed.returncode != 0:
        raise ReviewError(f"Editor exited {completed.returncode}. The draft was not changed.")
    title, body = parse_edit(path.read_text(encoding="utf-8"), draft.title)
    edit_draft(store, publisher, draft_id, title, body)
    print(f"Updated draft {draft_id}. It is pending approval.")
    return 0


def cmd_approve(
    store: Store,
    publishers: Mapping[str, Publisher],
    ids: list[int],
    note: str | None,
    gate: Gate,
) -> int:
    refused = False
    for draft_id in ids:
        draft = _one(store, draft_id)
        try:
            approve(store, _publisher(publishers, draft.platform), draft_id, note, gate=gate)
        except PrivacyRefused as exc:
            print(f"Draft {draft_id}: {exc}", file=sys.stderr)
            refused = True
            continue
        print(f"Approved {draft_id}.")
    return 1 if refused else 0


def cmd_compose(store: Store, publishers: list[Publisher], args: argparse.Namespace) -> int:
    text = args.text
    if text == "-" or (text is None and not sys.stdin.isatty()):
        text = sys.stdin.read()
    post = Post(
        kind=args.kind,
        subject=(args.subject or "").strip(),
        text=(text or "").strip(),
        link=(args.link or "").strip(),
        title=(args.title or "").strip(),
    )
    if args.platforms:
        known = {p.id for p in publishers}
        missing = [p for p in args.platforms if p not in known]
        if missing:
            raise ReviewError(f"Not enabled: {', '.join(missing)}")
        publishers = [p for p in publishers if p.id in args.platforms]
    ids = compose(store, publishers, post)
    for draft_id in ids:
        draft = store.get(draft_id)
        if draft:
            print(f"drafted #{draft.id} {draft.platform}")
    if ids:
        print("Review, edit and approve them like any draft.")
    return 0


def _release_ref(store: Store, ref: str):
    if "==" not in ref:
        raise ReviewError("Name a release as project==version")
    project, version = ref.split("==", 1)
    row = store.release(project, version)
    if row is None:
        raise ReviewError(f"{ref} isn't a collected release (digest mode collects releases as they're seen)")
    return row


def _digest_settings(config: Config):
    if config.digest is None:
        raise ReviewError("Digest mode is off. Add a digest: block to the config.")
    return config.digest


def cmd_digest(config: Config, store: Store, publishers: list[Publisher], args: argparse.Namespace) -> int:
    from pip_release.digest import build_digest, current_window, notable, period_label, window_of

    settings = _digest_settings(config)
    if args.action in ("exclude", "include"):
        for ref in args.refs:
            row = _release_ref(store, ref)
            store.set_excluded(row["id"], args.action == "exclude")
            print(f"{args.action}d {ref}")
        print("Run pip-release digest rebuild <id> to refresh a digest that was already drafted.")
        return 0
    if args.action == "rebuild":
        if not args.refs:
            raise ReviewError("Name the digest id to rebuild")
        try:
            gate = load_gate(config.privacy, os.environ)
        except GateUnavailable:
            gate = None
        for ref in args.refs:
            ids, status = build_digest(store, settings, publishers, int(ref), gate)
            print(f"digest {ref}: new drafts {', '.join(f'#{i}' for i in ids) or 'none'}; {status}")
        return 0
    now = datetime.now(timezone.utc)
    window = current_window(settings, now)
    open_rows = store.releases(open_only=True)
    print(f"Open window {period_label(window)} (closes {window.end}): {len(open_rows)} releases so far")
    suggested = {row["id"] for row, _line, flag in notable(store, open_rows) if flag}
    for row in open_rows:
        flags = (" [excluded]" if row["excluded"] else "") + (" [suggested headline]" if row["id"] in suggested else "")
        print(f"  {row['project']}=={row['version']}{flags}")
    for row in store.digests()[:5]:
        count = len([r for r in store.releases(digest_id=row["id"]) if not r["excluded"]])
        page = row["site_path"] or "no page"
        error = f"  ! {row['error']}" if row["error"] else ""
        print(f"digest {row['id']}  {period_label(window_of(row))}  {count} releases  {page}{error}")
    return 0


def cmd_promote(config: Config, store: Store, publishers: list[Publisher], args: argparse.Namespace) -> int:
    from pip_release.digest import promote

    row = _release_ref(store, args.ref)
    gate = None
    if args.site_page:
        gate = load_gate(config.privacy, os.environ)
    ids, status = promote(store, config.digest, publishers, row, site_page=args.site_page, gate=gate)
    print(f"headline drafts: {', '.join(f'#{i}' for i in ids) or 'already existed'}")
    if status:
        print(status)
    return 0


def cmd_check(store: Store, gate: Gate, ids: list[int]) -> int:
    drafts = [_one(store, i) for i in ids] if ids else [
        *store.list_drafts(status=PENDING), *store.list_drafts(status=APPROVED)
    ]
    found = False
    for draft in drafts:
        hits = gate_hits(store, gate, draft)
        if not hits:
            continue
        found = True
        print(f"#{draft.id} {draft.label} {draft.platform}")
        for hit in hits:
            print(f"  {hit.label:<24} {hit.field}[{hit.start}:{hit.end}] {hit.match!r}")
            print(f"  {'':<24} accept: pip-release accept {draft.id} {hit.key!r} --note '...'")
    if not found:
        print(f"No privacy hits in {len(drafts)} draft(s).")
    return 1 if found else 0


def cmd_reject(store: Store, ids: list[int], note: str | None) -> int:
    for draft_id in ids:
        reject(store, draft_id, note)
        print(f"Rejected {draft_id}.")
    return 0


def cmd_publish(
    config: Config,
    store: Store,
    publishers: Mapping[str, Publisher],
    http: HttpClient,
    ids: list[int],
    dry_run: bool,
    gate: Gate,
) -> int:
    failed = False
    for draft_id in ids:
        draft = _one(store, draft_id)
        publisher = _publisher(publishers, draft.platform)
        if dry_run:
            print(f"# {draft.platform} #{draft.id}")
            if draft.title:
                print(f"title: {draft.title}")
            print(draft.body)
            print()
            continue
        try:
            result = publish_draft(store, publisher, draft, os.environ, http, gate=gate)
        except (ReviewError, OSError, ValueError, HttpError) as exc:
            print(f"Draft {draft_id} failed: {exc}", file=sys.stderr)
            failed = True
            continue
        print(result.detail)
        if result.handed_off:
            current = store.get(draft_id) or draft
            print(_handoff(current, config.destinations.get(draft.platform), publisher))
    return 1 if failed else 0


def cmd_confirm(store: Store, draft_id: int, url: str | None = None) -> int:
    draft = confirm(store, draft_id, url)
    where = f" at {draft.external_id}" if draft.external_id else ""
    print(f"Marked {draft_id} published{where}.")
    if not draft.external_id:
        print(
            f"No post URL recorded. Add it later with: pip-release confirm {draft_id} --url <post url>",
            file=sys.stderr,
        )
    return 0


def cmd_healthcheck() -> int:
    raw = os.environ.get("PIP_RELEASE_HEARTBEAT")
    if not raw:
        print("PIP_RELEASE_HEARTBEAT is not set", file=sys.stderr)
        return 1
    path = Path(raw)
    if not path.is_file():
        print("No heartbeat yet", file=sys.stderr)
        return 1
    stamp, interval = path.read_text(encoding="utf-8").split()
    age = time.time() - float(stamp)
    if age > float(interval) * 2 + 30:
        print(f"Heartbeat is {age:.0f}s old", file=sys.stderr)
        return 1
    return 0


def parse_edit(text: str, previous_title: str) -> tuple[str, str]:
    normalized = text.replace("\r\n", "\n")
    lines = normalized.split("\n")
    if lines and lines[0].startswith("title:"):
        title = lines[0].split(":", 1)[1].strip()
        rest = lines[1:]
        if rest and rest[0] == "":
            rest = rest[1:]
        return title, "\n".join(rest).strip()
    return previous_title, normalized.strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pip-release",
        description="Draft PyPI release announcements and hold them for approval.",
    )
    parser.add_argument("--config", help="Path to YAML or JSON config. Default: config.yaml")
    parser.add_argument("--env", default=".env", help="Env file for secrets. Existing variables win.")
    parser.add_argument("--store", help="Override the SQLite path")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Copy the example config and .env into place")
    init.add_argument("--example", choices=("dragonhound",), help="Use the dragonhound fixture")
    init.add_argument("--force", action="store_true", help="Replace an existing config.yaml")

    discover = sub.add_parser("discover", help="Print projects from a PyPI user profile")
    discover.add_argument(
        "--check", action="store_true",
        help="Compare the configured projects with PyPI and GitHub; exit 1 on drift",
    )
    sub.add_parser("poll", help="Check PyPI once")
    sub.add_parser("watch", help="Poll on the configured interval")
    baseline = sub.add_parser(
        "baseline", help="Mark every current version as seen, without drafting"
    )
    baseline.add_argument("projects", nargs="*", help="Only these tracked projects")
    reset = sub.add_parser("reset", help="Clear the review queue")
    reset.add_argument(
        "--reject-pending", action="store_true", help="Reject pending drafts"
    )
    reset.add_argument("--before", help="Only drafts created before this ISO date or timestamp (UTC)")
    reset.add_argument("--note", default=STALE_NOTE, help=f"Default: {STALE_NOTE!r}")
    reset.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    sub.add_parser("platforms", help="Show destinations and missing secrets")
    sub.add_parser("healthcheck", help="Exit 0 when the watcher heartbeat is fresh")
    ui = sub.add_parser("ui", help="Open the review UI")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8787)

    drafts = sub.add_parser("drafts", help="List drafts")
    drafts.add_argument("--status", choices=("pending", "approved", "rejected", "published", "failed", "handed_off", "publishing"))

    show = sub.add_parser("show", help="Print one draft")
    show.add_argument("id", type=int)

    edit = sub.add_parser("edit", help="Edit a draft in $EDITOR")
    edit.add_argument("id", type=int)

    for name, help_text in (
        ("approve", "Approve drafts"),
        ("reject", "Reject drafts"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("ids", nargs="*", type=int)
        command.add_argument("--release", help="project==version")
        command.add_argument("--post", help="Every draft of one post, by post key")
        command.add_argument("--note")

    publish = sub.add_parser("publish", help="Publish approved drafts")
    publish.add_argument("ids", nargs="*", type=int)
    publish.add_argument("--release", help="project==version")
    publish.add_argument("--post", help="Every draft of one post, by post key")
    publish.add_argument("--approved", action="store_true", help="Every approved draft")
    publish.add_argument("--dry-run", action="store_true", help="Print the post and do not send it")

    collect_cmd = sub.add_parser("collect", help="Snapshot GitHub, PyPI and broadcast metrics into metrics.sqlite")
    collect_cmd.add_argument("--since", help="Only keep PyPI download days from this date (ISO)")

    digest_cmd = sub.add_parser("digest", help="Digest mode: the open window and past digests")
    digest_cmd.add_argument("action", nargs="?", default="status", choices=("status", "exclude", "include", "rebuild"))
    digest_cmd.add_argument("refs", nargs="*", help="project==version for exclude/include, digest ids for rebuild")

    promote_cmd = sub.add_parser("promote", help="Give one collected release its own headline post")
    promote_cmd.add_argument("ref", help="project==version")
    promote_cmd.add_argument("--site-page", action="store_true", help="Also write a kind: release site page")

    compose_cmd = sub.add_parser(
        "compose", help="Draft a post that isn't a release (demo, build note, video, page)"
    )
    compose_cmd.add_argument("text", nargs="?", help="The post text, or - to read stdin")
    compose_cmd.add_argument("--kind", choices=POST_KINDS, default="demo")
    compose_cmd.add_argument("--link", help="https:// link the post points to")
    compose_cmd.add_argument("--subject", help="Short label for the review queue")
    compose_cmd.add_argument("--title", help="Title, for platforms that have one (Patreon)")
    compose_cmd.add_argument(
        "--platform", dest="platforms", action="append", help="Only this destination (repeatable)"
    )

    check = sub.add_parser("check", help="Run the privacy gate over drafts (default: pending and approved)")
    check.add_argument("ids", nargs="*", type=int)

    accept = sub.add_parser("accept", help="Let one privacy hit through for one draft")
    accept.add_argument("id", type=int)
    accept.add_argument("key", help="The hit's key, as printed by check, e.g. 'email:a@b.org'")
    accept.add_argument("--note", required=True, help="Why it is safe to publish")

    confirm_cmd = sub.add_parser("confirm", help="Mark a manual handoff (X, Patreon) as published")
    confirm_cmd.add_argument("id", type=int)
    confirm_cmd.add_argument("--url", help="The live post's URL, kept for analytics")
    return parser


def _gate(config: Config) -> Gate:
    gate = load_gate(config.privacy, os.environ)
    for warning in gate.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return gate


def _selected(config: Config) -> list[Publisher]:
    known = configured_publishers(config)
    selected: list[Publisher] = []
    for name, destination in config.destinations.items():
        if name not in known:
            raise ConfigError(
                f"Unknown platform {name!r}. Installed: {', '.join(sorted(known))}"
            )
        if destination.enabled:
            selected.append(known[name])
    return selected


def _ids(args: argparse.Namespace, store: Store) -> list[int]:
    ids = list(getattr(args, "ids", []) or [])
    if getattr(args, "id", None):
        ids.append(args.id)
    if getattr(args, "approved", False):
        ids.extend(draft.id for draft in store.list_drafts(status=APPROVED))
    release = getattr(args, "release", None)
    if release:
        if "==" not in release:
            raise ReviewError("--release must look like project==version")
        project, version = release.split("==", 1)
        ids.extend(draft.id for draft in store.list_drafts(project=project, version=version))
    post_key = getattr(args, "post", None)
    if post_key:
        ids.extend(draft.id for draft in store.list_drafts(post_key=post_key))
    deduped = list(dict.fromkeys(ids))
    if not deduped:
        raise ReviewError("Name a draft id, --release, --post, or --approved.")
    return deduped


def _one(store: Store, draft_id: int) -> Draft:
    draft = store.get(draft_id)
    if draft is None:
        raise ReviewError(f"No draft {draft_id}")
    return draft


def _publisher(publishers: Mapping[str, Publisher], platform: str) -> Publisher:
    try:
        return publishers[platform]
    except KeyError as exc:
        raise ReviewError(f"No publisher named {platform}") from exc


def _format_event(event: PollEvent) -> str:
    if event.kind == "baseline":
        return f"baselined {event.project} ({event.detail}); no drafts"
    if event.kind == "draft":
        return f"drafted {event.project} {event.version}: {event.detail}"
    if event.kind == "collect":
        return f"collected {event.project} {event.version} for the next digest"
    if event.kind == "digest":
        return f"{event.project} to {event.version}: drafts {', '.join(f'#{i}' for i in event.draft_ids) or 'none new'}; {event.detail}"
    if event.kind == "skip":
        return f"skipped {event.project} {event.version}: {event.detail}"
    return f"error {event.project}: {event.detail}"


def _print_draft(draft: Draft) -> None:
    print(f"#{draft.id}  {draft.kind}  {draft.label}  {draft.platform}  {draft.status}")
    if draft.title:
        print(f"title: {draft.title}")
    print()
    print(draft.body)
    if draft.error:
        print(f"\nerror: {draft.error}")
    if draft.note:
        print(f"\nnote: {draft.note}")


def _handoff(draft: Draft, destination: Destination | None, publisher: Publisher | None = None) -> str:
    link = publisher.handoff_url(draft) if publisher else None
    if link:
        return f"Open in {publisher.label}:\n{link}\n\nText:\n{draft.body}\n"
    where = (destination.url if destination and destination.url else "https://www.patreon.com")
    title = draft.title or draft.label
    return (
        f"Paste this into {where}\n\n"
        f"Title:\n{title}\n\n"
        f"{plain_to_html(draft.body)}\n"
    )


def _heartbeat(config: Config) -> None:
    raw = os.environ.get("PIP_RELEASE_HEARTBEAT")
    if not raw:
        return
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{time.time():.0f} {config.interval_seconds}\n", encoding="utf-8")


def _copy(source: Path, dest: Path, force: bool) -> None:
    if dest.exists() and not force:
        return
    shutil.copyfile(source, dest)


def _temp_path(draft_id: int) -> Path:
    directory = Path(os.environ.get("TMPDIR") or "/tmp")
    return directory / f"pip-release-{draft_id}.txt"
