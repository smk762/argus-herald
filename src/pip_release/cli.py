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
from datetime import datetime, timedelta, timezone
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
    LOGGED_KINDS,
    accept_hit,
    attach_media,
    edit_logged,
    log_broadcast,
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
        return cmd_approve(store, by_id, _ids(args, store), args.note, _gate(config),
                           schedule=not args.no_schedule, schedules=_schedules(config))
    if args.command == "reject":
        return cmd_reject(store, _ids(args, store), args.note)
    if args.command == "publish":
        return cmd_publish(config, store, by_id, http, _ids(args, store), args.dry_run, _gate(config))
    if args.command == "media":
        return cmd_media(config, store, args)
    if args.command == "log-broadcast":
        return cmd_log_broadcast(store, args)
    if args.command == "broadcasts":
        return cmd_broadcasts(store, args)
    if args.command == "import":
        return cmd_import(config, args)
    if args.command == "followers":
        return cmd_followers(config)
    if args.command == "collect":
        return cmd_collect(config, store, http, args.since)
    if args.command == "digest":
        return cmd_digest(config, store, publishers, args)
    if args.command == "schedule":
        return cmd_schedule(config, store, args)
    if args.command == "scheduled":
        return cmd_scheduled(store)
    if args.command == "slots":
        from pip_release.metrics import metrics_path
        from pip_release.schedule import suggest

        print(suggest(metrics_path(config)))
        return 0
    if args.command == "regenerate":
        return cmd_regenerate(config, store, by_id, args.id, args.force)
    if args.command == "promote":
        return cmd_promote(config, store, publishers, args)
    if args.command == "compose":
        return cmd_compose(config, store, publishers, args)
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


def cmd_media(config: Config, store: Store, args: argparse.Namespace) -> int:
    if args.action == "add":
        if len(args.args) != 2 or not args.alt:
            raise ReviewError("Usage: pip-release media add <draft id> <file> --alt '...' [--all-drafts]")
        draft_id, path = int(args.args[0]), Path(args.args[1]).expanduser()
        ids = attach_media(store, config.media_root, draft_id, path, args.alt, all_drafts=args.all_drafts)
        item = store.media_item(ids[0])
        print(f"Attached {path.name} as {item['kind']} to {len(ids)} draft(s); removed: {item['removed']}")
        return 0
    if args.action == "list":
        for item in store.media(int(args.args[0])):
            print(f"#{item['id']:<4} {item['kind']:<6} {item['bytes'] // 1000:>7} kB  {item['name']}")
            print(f"      alt: {item['alt'] or '(missing)'}   removed: {item['removed']}")
        return 0
    if args.action == "alt":
        if len(args.args) != 2 or not store.set_alt(int(args.args[0]), args.args[1].strip()):
            raise ReviewError("Usage: pip-release media alt <media id> 'text'")
        print("Saved.")
        return 0
    if args.action == "remove":
        if not args.args or not store.remove_media(int(args.args[0])):
            raise ReviewError("Usage: pip-release media remove <media id>")
        print("Removed.")
        return 0
    raise ReviewError(f"Unknown media action {args.action}")


def cmd_log_broadcast(store: Store, args: argparse.Namespace) -> int:
    fields = {"platform": args.platform, "url": args.url, "kind": args.kind, "project": args.project,
              "subject": args.subject, "text": args.text, "at": args.at}
    if args.id:
        edit_logged(store, args.id, **fields)
        print(f"Updated logged broadcast {args.id}.")
        return 0
    if not args.platform or not args.url:
        raise ReviewError("--platform and --url are required (or --id to edit one)")
    defaults = {"kind": "other", "project": "", "subject": "", "text": ""}
    broadcast_id = log_broadcast(store, **{k: (v if v is not None else defaults.get(k)) for k, v in fields.items()})
    row = store.logged_broadcast(broadcast_id)
    print(f"Logged #{broadcast_id}: {row['platform']} {row['url']} at {row['at']}")
    return 0


def cmd_broadcasts(store: Store, args: argparse.Namespace) -> int:
    if args.action == "delete":
        if not args.ids:
            raise ReviewError("Name the logged broadcast ids to delete")
        for broadcast_id in args.ids:
            if not store.delete_logged(broadcast_id):
                raise ReviewError(f"No logged broadcast {broadcast_id}")
            print(f"Deleted logged broadcast {broadcast_id}.")
        return 0
    rows = store.logged_broadcasts()
    if not rows:
        print("No broadcasts logged by hand.")
    for row in rows:
        label = f"  {row['subject']}" if row["subject"] else ""
        print(f"#{row['id']:<4} {row['at'][:16]}  {row['platform']:<12} {row['kind']:<9} {row['url']}{label}")
    return 0


def cmd_import(config: Config, args: argparse.Namespace) -> int:
    from datetime import date as _date

    from pip_release.metrics import Metrics, metrics_path, record_account

    account = (args.account or config.accounts.x or "").lstrip("@")
    if not account:
        raise ReviewError("Name the X account with --account, or set destinations.twitter.username")

    def day(value: str | None, flag: str) -> _date:
        try:
            return _date.fromisoformat(value) if value else datetime.now(timezone.utc).date()
        except ValueError as exc:
            raise ReviewError(f"--{flag} must look like 2026-11-15") from exc

    counts = ("followers", "following", "posts", "impressions", "engagements", "link_clicks", "profile_visits",
              "new_follows", "likes", "reposts", "replies")
    for name in counts:
        value = getattr(args, name, None)
        if value is not None and value < 0:
            raise ReviewError(f"--{name.replace('_', '-')} can't be negative")
    if args.what == "x-analytics":
        if not args.file:
            raise ReviewError("Give the CSV file: pip-release import x-analytics <file.csv>")
        try:
            text = Path(args.file).expanduser().read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise ReviewError(f"Can't read {args.file}: {exc}") from exc
    elif args.file:
        raise ReviewError(f"import {args.what} doesn't take a file")
    metrics = Metrics(metrics_path(config))
    try:
        if args.what == "x-account":
            if args.followers is None:
                raise ReviewError("import x-account needs --followers")
            when = day(args.date, "date")
            record_account(metrics, "x", account, when.isoformat(), args.followers, args.following, args.posts)
            print(f"Recorded X @{account} on {when}: {args.followers} followers")
        elif args.what == "x-week":
            from pip_release.engagement import record_x_week

            end = day(args.end, "end")
            start = day(args.start, "start") if args.start else end - timedelta(days=6)
            if start > end:
                raise ReviewError("--start is after --end")
            values = {k: getattr(args, k) for k in counts[3:]}
            if all(v is None for v in values.values()):
                raise ReviewError("Give at least one number, e.g. --impressions 12000")
            record_x_week(metrics, account, start, end, **values)
            print(f"Recorded X @{account} {start} to {end}: "
                  + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in values.items() if v is not None))
        else:
            from pip_release.engagement import import_x_analytics

            try:
                result = import_x_analytics(metrics, text, account)
            except ValueError as exc:
                raise ReviewError(str(exc)) from exc
            if result.posts:
                print(f"Imported {result.posts} X posts ({result.matched} matched to herald broadcasts)")
            if result.days:
                print(f"Imported {result.days} days of X account totals")
            for line in result.skipped[:10]:
                print(f"skipped {line}", file=sys.stderr)
            if len(result.skipped) > 10:
                print(f"skipped {len(result.skipped) - 10} more lines", file=sys.stderr)
    finally:
        metrics.close()
    return 0


def cmd_followers(config: Config) -> int:
    from pip_release.metrics import Metrics, metrics_path

    path = metrics_path(config)
    if not path.exists():
        print("No metrics yet. Run pip-release collect.")
        return 0
    metrics = Metrics(path)
    try:
        rows = metrics.conn.execute(
            "SELECT platform, account, date, followers, following, posts FROM account_metrics a "
            "WHERE date = (SELECT MAX(date) FROM account_metrics b WHERE b.platform = a.platform AND b.account = a.account) "
            "ORDER BY platform"
        ).fetchall()
        if not rows:
            print("No follower counts yet. Add accounts: to the config, or import x-account for X.")
        for row in rows:
            week = metrics.conn.execute(
                "SELECT followers FROM account_metrics WHERE platform = ? AND account = ? AND date <= date(?, '-7 days') "
                "AND followers IS NOT NULL ORDER BY date DESC LIMIT 1",
                (row["platform"], row["account"], row["date"]),
            ).fetchone()
            change = ""
            if week and row["followers"] is not None:
                change = f"  {row['followers'] - week['followers']:+d} in 7 days"
            followers = "hidden" if row["followers"] is None else row["followers"]
            print(f"{row['platform']:<9} {row['account']:<28} {row['date']}  {followers} followers{change}")
    finally:
        metrics.close()
    return 0


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


ENGAGEMENT_KEY = "engagement_at"
ENGAGEMENT_EVERY = 3600


def _engagement_hourly(config: Config, store: Store, http: HttpClient) -> None:
    """From the watcher: posts are sampled at +1h, +24h, +72h and +7d, so this runs hourly."""
    last = store.get_meta(ENGAGEMENT_KEY)
    now = time.time()
    if last and now - float(last) < ENGAGEMENT_EVERY:
        return
    from pip_release.metrics import Metrics, collect_engagement, metrics_path

    metrics = Metrics(metrics_path(config))
    try:
        report = collect_engagement(store, metrics, http)
    finally:
        metrics.close()
    store.set_meta(ENGAGEMENT_KEY, str(now))
    for line in report.errors:
        log.warning("engagement: %s", line)


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
        audience = destination.audience
        if audience:
            bits = []
            if audience.include or audience.exclude:
                bits.append("packages " + ", ".join(audience.include or ("*",)) + (f" not {', '.join(audience.exclude)}" if audience.exclude else ""))
            if audience.link_placement == "reply":
                bits.append("link in reply")
            if audience.hashtags:
                bits.append(" ".join(f"#{t}" for t in audience.hashtags))
            if audience.tone:
                bits.append(f"tone: {audience.tone}")
            if audience.description:
                bits.append(audience.description)
            print(f"{'':<16} audience: {'; '.join(bits)}")
    _metrics_sources(config, env)
    return 0


def _metrics_sources(config: Config, env: Mapping[str, str]) -> None:
    """What `collect` can gather, and what each missing piece would add."""
    from pip_release.engagement import site_configured

    accounts = config.accounts
    rows = [
        ("GitHub traffic", "" if env.get("GITHUB_TOKEN", "").strip() else "missing GITHUB_TOKEN"),
        ("Bluesky", accounts.bluesky or "set accounts.bluesky"),
        ("Mastodon", accounts.mastodon or "set accounts.mastodon"),
        ("YouTube subs", "" if env.get("YOUTUBE_API_KEY", "").strip() else "missing YOUTUBE_API_KEY"),
        ("X", "manual: import x-account, x-analytics, x-week"),
    ]
    missing = site_configured(env)
    rows.append(("Site traffic", f"missing {', '.join(missing)}" if missing else ""))
    print("\nmetrics:")
    for label, state in rows:
        print(f"  {label:<14} {state or 'ok'}")


def _schedules(config: Config) -> dict:
    return {name: d.schedule for name, d in config.destinations.items() if d.schedule is not None}


def cmd_schedule(config: Config, store: Store, args: argparse.Namespace) -> int:
    from pip_release.schedule import free_slots, iso, parse

    draft = _one(store, args.id)
    if draft.status != APPROVED:
        raise ReviewError(f"Draft {draft.id} is {draft.status}; approve it before scheduling")
    if args.clear:
        store.set_publish_at(draft.id, None)
        print(f"Draft {draft.id} is no longer scheduled; it goes out when you publish it.")
        return 0
    if args.at:
        try:
            when = parse(args.at)
        except ValueError as exc:
            raise ReviewError("--at must be an ISO time, e.g. 2026-12-01T08:00Z") from exc
        if when <= datetime.now(timezone.utc):
            raise ReviewError("That time has passed; use publish to post now")
    else:
        schedule = _schedules(config).get(draft.platform)
        if schedule is None:
            raise ReviewError(f"{draft.platform} has no schedule in the config; pass --at")
        slots = free_slots(store, draft.platform, schedule, datetime.now(timezone.utc), count=1, ignore=draft.id)
        if not slots:
            raise ReviewError("No free slot in the next weeks")
        when = slots[0]
    store.set_publish_at(draft.id, iso(when))
    print(f"Draft {draft.id} goes out {iso(when)}.")
    return 0


def cmd_scheduled(store: Store) -> int:
    drafts = store.scheduled()
    if not drafts:
        print("Nothing scheduled.")
    for draft in drafts:
        print(f"{draft.publish_at}  #{draft.id:<5} {draft.platform:<10} {draft.label}")
    return 0


def _publish_due(config: Config, store: Store, http: HttpClient) -> None:
    """Publish or hand off scheduled drafts that are due, and say so."""
    from pip_release.schedule import publish_due

    if not store.scheduled():
        return
    try:
        gate = load_gate(config.privacy, os.environ)
    except GateUnavailable:
        gate = None
    results = publish_due(store, configured_publishers(config), _schedules(config), os.environ, http, gate,
                          datetime.now(timezone.utc))
    for result in results:
        print(f"scheduled #{result.draft.id} {result.draft.platform}: {result.kind}: {result.detail}")
    handed = [r.draft for r in results if r.kind == "handed_off"]
    if handed:
        _notify_text("Time to post", "\n".join(f"{d.platform}: {d.label}" for d in handed[:6]) + "\nOpen the review page to post it.")
    problems = [r for r in results if r.kind in ("missed", "failed")]
    if problems:
        _notify_text("Scheduled post needs you", "\n".join(f"{r.draft.platform}: {r.draft.label}: {r.kind}" for r in problems[:6]))


def cmd_poll(config: Config, store: Store, http: HttpClient, publishers: list[Publisher]) -> int:
    _publish_due(config, store, http)
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
    last_poll = 0.0
    while True:
        now = time.time()
        if now - last_poll < config.interval_seconds:
            # Woken for a scheduled post between polls.
            try:
                _publish_due(config, store, http)
            except (ConfigError, ReviewError, HttpError, OSError, ValueError) as exc:
                log.error("%s", exc)
            time.sleep(_sleep_for(config, store, last_poll))
            continue
        last_poll = now
        try:
            cmd_poll(config, store, http, publishers)
            _discover_daily(config, store, http)
            _collect_daily(config, store, http)
            _engagement_hourly(config, store, http)
        except (ConfigError, ReviewError, HttpError, OSError, ValueError) as exc:
            log.error("%s", exc)
            _heartbeat(config)
        time.sleep(_sleep_for(config, store, last_poll))


def _sleep_for(config: Config, store: Store, last_poll: float) -> float:
    """Until the next poll, or the next scheduled post if that's sooner (at least 30 s)."""
    from pip_release.schedule import next_due_in

    until_poll = max(0.0, last_poll + config.interval_seconds - time.time())
    due = next_due_in(store, datetime.now(timezone.utc))
    wait = until_poll if due is None else min(until_poll, due + 1)
    return max(30.0, wait)


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
    _print_draft(draft, store.media(draft_id))
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
    *,
    schedule: bool = True,
    schedules: dict | None = None,
) -> int:
    refused = False
    for draft_id in ids:
        draft = _one(store, draft_id)
        try:
            updated = approve(store, _publisher(publishers, draft.platform), draft_id, note, gate=gate,
                              schedule=schedule, schedules=schedules)
        except PrivacyRefused as exc:
            print(f"Draft {draft_id}: {exc}", file=sys.stderr)
            refused = True
            continue
        when = f", scheduled for {updated.publish_at}" if updated.publish_at else ""
        print(f"Approved {draft_id}{when}.")
    return 1 if refused else 0


def cmd_compose(config: Config, store: Store, publishers: list[Publisher], args: argparse.Namespace) -> int:
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
    media = list(zip(args.media or [], args.alt or []))
    if len(args.media or []) != len(args.alt or []):
        raise ReviewError("Give one --alt for each --media, in the same order")
    ids = compose(store, publishers, post)
    if ids and media:
        for path, alt in media:
            attach_media(store, config.media_root, ids[0], Path(path).expanduser(), alt, all_drafts=True)
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
        backend = _backend(config)
        for ref in args.refs:
            ids, status = build_digest(store, settings, publishers, int(ref), gate, backend)
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
    backend = _backend(config)
    if backend is not None and backend.remote and gate is None:
        try:
            gate = load_gate(config.privacy, os.environ)
        except GateUnavailable:
            gate = None
    ids, status = promote(store, config.digest, publishers, row, site_page=args.site_page, gate=gate, backend=backend)
    print(f"headline drafts: {', '.join(f'#{i}' for i in ids) or 'already existed'}")
    if status:
        print(status)
    return 0


def _backend(config: Config):
    from pip_release.drafting import configured_backend

    backend, warning = configured_backend(config.drafting, os.environ, HttpClient())
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    return backend


def cmd_regenerate(config: Config, store: Store, publishers: Mapping[str, Publisher], draft_id: int, force: bool) -> int:
    from pip_release.digest import regenerate

    draft = _one(store, draft_id)
    try:
        gate = load_gate(config.privacy, os.environ)
    except GateUnavailable:
        gate = None
    try:
        message = regenerate(store, config.digest, _publisher(publishers, draft.platform), draft_id,
                             _backend(config), gate, force=force)
    except ValueError as exc:
        raise ReviewError(str(exc)) from exc
    print(message)
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
            for item in store.media(draft_id):
                print(f"Attach: {item['path']}\n  alt text: {item['alt']}")
    return 1 if failed else 0


def cmd_confirm(store: Store, draft_id: int, url: str | None = None) -> int:
    from pip_release.platforms import TwitterPublisher

    draft = confirm(store, draft_id, url)
    where = f" at {draft.external_id}" if draft.external_id else ""
    print(f"Marked {draft_id} published{where}.")
    reply = TwitterPublisher().reply_url(draft) if draft.platform == "twitter" else None
    if reply:
        print(f"Now post the link as a reply:\n{reply}")
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
        if name == "approve":
            command.add_argument("--no-schedule", action="store_true",
                                 help="Don't give it a slot; it goes out when you publish it")
        command.add_argument("--release", help="project==version")
        command.add_argument("--post", help="Every draft of one post, by post key")
        command.add_argument("--note")

    publish = sub.add_parser("publish", help="Publish approved drafts")
    publish.add_argument("ids", nargs="*", type=int)
    publish.add_argument("--release", help="project==version")
    publish.add_argument("--post", help="Every draft of one post, by post key")
    publish.add_argument("--approved", action="store_true", help="Every approved draft")
    publish.add_argument("--dry-run", action="store_true", help="Print the post and do not send it")

    logb = sub.add_parser("log-broadcast", help="Record a post made outside herald (Reddit, HN, YouTube, ...)")
    logb.add_argument("--platform", help="Short name: reddit, hn, youtube, newsletter, x, ...")
    logb.add_argument("--url", help="The live post")
    logb.add_argument("--kind", choices=LOGGED_KINDS)
    logb.add_argument("--project", help="The package it was about, if one")
    logb.add_argument("--subject", help="Short label")
    logb.add_argument("--text", help="What it said, for the dashboard")
    logb.add_argument("--at", help="When it was posted (ISO date or timestamp; default now, UTC)")
    logb.add_argument("--id", type=int, help="Edit this logged broadcast instead of adding one")

    bcasts = sub.add_parser("broadcasts", help="List or delete broadcasts logged by hand")
    bcasts.add_argument("action", nargs="?", default="list", choices=("list", "delete"))
    bcasts.add_argument("ids", nargs="*", type=int)

    imp = sub.add_parser(
        "import", help="Record X numbers (no free API): follower counts, the analytics CSV, or a week's totals"
    )
    imp.add_argument("what", choices=("x-account", "x-analytics", "x-week"))
    imp.add_argument("file", nargs="?", help="x-analytics: the CSV exported from X analytics")
    imp.add_argument("--date", help="x-account: day the numbers are for (default today, UTC)")
    imp.add_argument("--followers", type=int)
    imp.add_argument("--following", type=int)
    imp.add_argument("--posts", type=int)
    imp.add_argument("--start", help="x-week: first day (default 6 days before --end)")
    imp.add_argument("--end", help="x-week: last day (default today, UTC)")
    for flag in ("impressions", "engagements", "link-clicks", "profile-visits", "new-follows", "likes", "reposts",
                 "replies"):
        imp.add_argument(f"--{flag}", type=int, help="x-week" if flag == "impressions" else argparse.SUPPRESS)
    imp.add_argument("--account", help="X username (default destinations.twitter.username)")
    sub.add_parser("followers", help="Latest follower counts per platform, with the 7-day change")

    collect_cmd = sub.add_parser("collect", help="Snapshot GitHub, PyPI and broadcast metrics into metrics.sqlite")
    collect_cmd.add_argument("--since", help="Only keep PyPI download days from this date (ISO)")

    digest_cmd = sub.add_parser("digest", help="Digest mode: the open window and past digests")
    digest_cmd.add_argument("action", nargs="?", default="status", choices=("status", "exclude", "include", "rebuild"))
    digest_cmd.add_argument("refs", nargs="*", help="project==version for exclude/include, digest ids for rebuild")

    sched = sub.add_parser("schedule", help="Set when an approved draft goes out")
    sched.add_argument("id", type=int)
    group = sched.add_mutually_exclusive_group()
    group.add_argument("--at", help="ISO time (UTC if no offset), e.g. 2026-12-01T08:00Z")
    group.add_argument("--next", action="store_true", help="The destination's next free slot (default)")
    group.add_argument("--clear", action="store_true", help="Unschedule: it goes out when you publish it")
    sub.add_parser("scheduled", help="List scheduled drafts")
    slots = sub.add_parser("slots", help="Suggest posting times from engagement data")
    slots.add_argument("action", choices=("suggest",))

    regen = sub.add_parser("regenerate", help="Draft a digest or headline post again (with the drafting backend)")
    regen.add_argument("id", type=int)
    regen.add_argument("--force", action="store_true", help="Replace a draft a person has edited")

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
    compose_cmd.add_argument("--media", action="append", help="Image, GIF or clip to attach (repeatable)")
    compose_cmd.add_argument("--alt", action="append", help="Alt text for each --media, in order")

    media_cmd = sub.add_parser("media", help="Attach images, GIFs or clips to a draft (metadata is stripped)")
    media_cmd.add_argument("action", choices=("add", "list", "alt", "remove"))
    media_cmd.add_argument("args", nargs="*", help="add: <draft id> <file>; list: <draft id>; alt: <media id> 'text'; remove: <media id>")
    media_cmd.add_argument("--alt", help="Alt text (required for add)")
    media_cmd.add_argument("--all-drafts", action="store_true", help="Attach to every draft of the same post")

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


def _print_draft(draft: Draft, media: list[dict] | None = None) -> None:
    print(f"#{draft.id}  {draft.kind}  {draft.label}  {draft.platform}  {draft.status}")
    if draft.title:
        print(f"title: {draft.title}")
    print()
    print(draft.body)
    if draft.error:
        print(f"\nerror: {draft.error}")
    if draft.note:
        print(f"\nnote: {draft.note}")
    for item in media or []:
        print(f"\nmedia #{item['id']} {item['kind']}: {item['path']}\n  alt: {item['alt'] or '(missing)'}")


def _handoff(draft: Draft, destination: Destination | None, publisher: Publisher | None = None) -> str:
    link = publisher.handoff_url(draft) if publisher else None
    if link:
        then = ("\nThen confirm with --url; you'll get a link to post the reply:\n" + draft.reply + "\n") if draft.reply else ""
        return f"Open in {publisher.label}:\n{link}\n\nText:\n{draft.body}\n{then}"
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
