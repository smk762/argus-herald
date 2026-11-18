"""Run the review UI on a fresh, seeded store for Bombadil to explore.

    python tests/bombadil/serve.py [--token TOKEN] [--ready-file PATH]

Everything lives in a new temporary directory: config, store, media and a fixture deny-list (never
the real one). Publishers get a fake HTTP client, so nothing leaves the machine, and the drafting
backend is the template, so no model is called. The port is picked by the OS and bound before the
URL is announced, so the tests can never land on another program that happens to be listening.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import socket
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from PIL import Image  # noqa: E402

from argus_herald.config import load_config  # noqa: E402
from argus_herald.store import Store  # noqa: E402
from argus_herald.workflow import attach_media  # noqa: E402

DENY_TERM = "zebra-canyon-7"  # the fixture deny-list's only term

CONFIG = """
pypi:
  projects: [argus-atlas, argus-lens]
destinations:
  twitter:
    enabled: true
    username: herald_test
    schedule:
      slots: ["Tue 14:00", "Thu 14:00", "Sat 10:00"]
      min_gap_hours: 12
  discord:
    enabled: true
    schedule:
      slots: ["Mon 09:00", "Wed 09:00", "Fri 09:00"]
  telegram:
    enabled: true
  patreon:
    enabled: true
    name: HeraldTest
    url: https://www.patreon.com/c/HeraldTest
"""


class FakeHttp:
    """Every publisher call succeeds without a network connection."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def request(self, method, url, body=None, headers=None):  # noqa: ANN001
        self.calls.append(f"{method} {url}")
        return 200, json.dumps({"id": "1", "ok": True, "result": {"message_id": 1}}).encode()


def _image(path: Path, color: tuple[int, int, int]) -> Path:
    Image.new("RGB", (64, 48), color).save(path)
    return path


def seed(root: Path):
    (root / "config.yaml").write_text(CONFIG, encoding="utf-8")
    config = load_config(root / "config.yaml")
    config.store_path = root / "state.sqlite"
    media_root = config.media_root
    media_root.mkdir(parents=True, exist_ok=True)
    _image(media_root / "chart.png", (14, 95, 92))
    _image(media_root / "banner.png", (140, 58, 43))
    upload = _image(root / "screenshot.png", (200, 180, 120))

    store = Store(config.store_path)
    link = "https://pypi.org/project/argus-atlas/0.3.0/"
    body = "argus-atlas 0.3.0 is out: faster tile loading.\n\n" + link

    def draft(platform: str, text: str = body, **kw) -> int:
        kw.setdefault("project", "argus-atlas")
        kw.setdefault("version", "0.3.0")
        title = kw.pop("title", "argus-atlas 0.3.0" if platform == "patreon" else "")
        return store.add_draft(platform=platform, title=title, body=text, link=link, **kw)[0]

    # Pending, clean: approve works.
    draft("discord")
    draft("telegram")
    # Pending with a privacy-gate hit: approve must stay disabled until it's edited out or accepted.
    draft("twitter", f"argus-atlas 0.3.0 is out. Built on {DENY_TERM}.\n\n{link}")
    # Pending with an attachment that has no alt text.
    patreon = draft("patreon")
    attach_media(store, media_root, patreon, upload, "")
    # Approved and scheduled; approved and not.
    lens = {"project": "argus-lens", "version": "1.1.0"}
    for platform in ("twitter", "discord"):
        did = draft(platform, "argus-lens 1.1.0: a new heatmap view.", **lens)
        store.transition(did, ("pending",), "approved")
    store.set_publish_at(store.list_drafts(post_key="release:argus-lens==1.1.0")[0].id, "2031-01-07T14:00:00+00:00")
    # Handed off to a person (X), published, rejected, failed.
    old = {"project": "argus-atlas", "version": "0.2.0"}
    handed = draft("twitter", "argus-atlas 0.2.0 is out.", **old)
    store.transition(handed, ("pending",), "handed_off")
    published = draft("discord", "argus-atlas 0.2.0 is out.", **old)
    store.transition(published, ("pending",), "published", external_id="1")
    rejected = draft("telegram", "argus-atlas 0.2.0 is out.", **old)
    store.transition(rejected, ("pending",), "rejected", note="Too early")
    failed = draft("patreon", "argus-atlas 0.2.0 is out.", **old)
    store.transition(failed, ("pending",), "failed", error="Patreon was down")
    # Digest and headline drafts (Regenerate uses the template backend here).
    store.add_draft(
        platform="discord",
        title="",
        body="Two weeks of Argus: atlas 0.3.0, lens 1.1.0.",
        link="",
        kind="digest",
        post_key="digest:2026-11-16",
        subject="Digest 16 Nov",
    )
    store.add_draft(
        platform="telegram",
        title="",
        body="Argus Lens 1.1.0 adds heatmaps.",
        link="",
        kind="headline",
        post_key="headline:argus-lens==1.1.0",
        subject="Lens 1.1.0",
    )
    store.conn.commit()
    store.close()
    return config


def seed_metrics(path: Path, today: date | None = None) -> None:
    """Four months of plausible metrics, with bumps after some posts and overlapping posts."""
    import math
    import random

    from argus_herald.metrics import Metrics

    # Snapshots exist up to the end of the injected day, not the wall clock: a fixed `today` gives fixed data.
    now = datetime.now(UTC) if today is None else datetime.combine(today, datetime.max.time(), UTC)
    today = today or now.date()
    rng = random.Random(7)
    m = Metrics(path)
    days = [today - timedelta(days=i) for i in range(120, -1, -1)]
    posts = [  # (days ago, hour, platform, url, kind, subject, media, drafted_by)
        (
            40,
            14,
            "twitter",
            "https://x.com/herald_test/status/1900000000000000001",
            "release",
            "atlas 0.2.0",
            1,
            "template",
        ),
        (40, 15, "discord", "draft:101", "release", "atlas 0.2.0", 0, "template"),
        (
            25,
            9,
            "bluesky",
            "https://bsky.app/profile/herald.test/post/3kaaa",
            "digest",
            "Digest 1",
            1,
            "openai_compatible:llama3.1",
        ),
        (
            24,
            10,
            "mastodon",
            "https://mapstodon.space/@herald/113000000000000001",
            "digest",
            "Digest 1",
            0,
            "openai_compatible:llama3.1",
        ),
        (12, 16, "reddit", "https://www.reddit.com/r/Python/comments/abc/", "demo", "Atlas demo", 1, "logged"),
        (
            11,
            14,
            "twitter",
            "https://x.com/herald_test/status/1900000000000000002",
            "headline",
            "lens 1.1.0",
            1,
            "anthropic:claude",
        ),
        (5, 8, "bluesky", "https://bsky.app/profile/herald.test/post/3kbbb", "note", "Build note", 0, "template"),
    ]
    boost = {}
    for ago, *_ in posts:
        for k in range(3):
            boost[today - timedelta(days=ago - k)] = boost.get(today - timedelta(days=ago - k), 0) + (3 - k) * 9
    rows = []
    for i, day in enumerate(days):
        for package, base in (("argus-atlas", 40), ("argus-lens", 12)):
            wave = base * (1 + 0.3 * math.sin(i / 3.5)) + rng.randint(0, base // 3) + boost.get(day, 0)
            rows.append({"package": package, "date": day.isoformat(), "downloads": int(wave)})
    m.upsert("pypi_downloads", rows)
    traffic, counts, stars = [], [], {"argus-atlas": 30, "argus-lens": 8}
    for day in days[-45:]:
        for repo in stars:
            b = boost.get(day, 0)
            traffic.append(
                {
                    "repo": repo,
                    "date": day.isoformat(),
                    "views": 20 + rng.randint(0, 15) + b,
                    "uniques": 6 + rng.randint(0, 5) + b // 3,
                    "clones": rng.randint(0, 6),
                    "clone_uniques": rng.randint(0, 3),
                }
            )
            stars[repo] += rng.random() < (0.5 if b else 0.15)
            counts.append({"repo": repo, "date": day.isoformat(), "stars": stars[repo]})
    m.upsert("gh_traffic", traffic)
    m.upsert("gh_star_counts", counts)
    refs = []
    for day in days[-30:]:
        for referrer, base in (
            ("github.com", 12),
            ("t.co", 4),
            ("bsky.app", 3),
            ("mapstodon.space", 1),
            ("www.reddit.com", 2),
            ("google.com", 5),
            ("news.ycombinator.com", 0),
            ("herald-test.example", 2),
        ):
            u = (
                base
                + rng.randint(0, 3)
                + (boost.get(day, 0) // 6 if referrer in ("t.co", "bsky.app", "www.reddit.com") else 0)
            )
            if u:
                refs.append(
                    {
                        "repo": "argus-atlas",
                        "snapshot_date": day.isoformat(),
                        "referrer": referrer,
                        "count": u * 2,
                        "uniques": u,
                    }
                )
    m.upsert("gh_referrers", refs)
    m.upsert(
        "site_traffic",
        [
            {
                "date": d_.isoformat(),
                "path": "/updates/",
                "pageviews": 30 + rng.randint(0, 20) + boost.get(d_, 0),
                "visits": 20 + rng.randint(0, 10) + boost.get(d_, 0) // 2,
            }
            for d_ in days[-28:]
        ],
    )
    events = [
        {
            "kind": "release",
            "source": "pypi",
            "project": pkg,
            "version": ver,
            "at": f"{(today - timedelta(days=ago)).isoformat()}T12:00:00+00:00",
            "url": f"https://pypi.org/project/{pkg}/{ver}/",
            "detail": "",
        }
        for pkg, ver, ago in (("argus-atlas", "0.2.0", 41), ("argus-lens", "1.1.0", 12), ("argus-atlas", "0.3.0", 3))
    ]
    metrics_rows = []
    for ago, hour, platform, url, kind, subject, media, drafted_by in posts:
        at = datetime.combine(today - timedelta(days=ago), datetime.min.time(), UTC) + timedelta(hours=hour)
        events.append(
            {
                "kind": "broadcast",
                "source": platform,
                "project": "argus-atlas" if "atlas" in subject else "",
                "version": "",
                "at": at.isoformat(),
                "url": url,
                "detail": json.dumps(
                    {
                        "kind": kind,
                        "subject": subject,
                        "text": f"{subject}: what changed and why.",
                        "logged": drafted_by == "logged",
                        "drafted_by": drafted_by,
                        "post": f"post:{subject}",
                        "media": media,
                    }
                ),
            }
        )
        if platform in ("bluesky", "mastodon"):
            for hours, scale in ((1, 1), (24, 4), (72, 6), (168, 7)):
                if at + timedelta(hours=hours) < now:
                    metrics_rows.append(
                        {
                            "platform": platform,
                            "external_id": url,
                            "fetched_at": (at + timedelta(hours=hours, minutes=3)).isoformat(),
                            "likes": 2 * scale,
                            "reposts": scale,
                            "replies": scale // 2,
                            "quotes": 0,
                            "impressions": None,
                            "engagements": None,
                            "link_clicks": None,
                            "profile_visits": None,
                            "source": "api",
                        }
                    )
        if platform == "twitter":
            metrics_rows.append(
                {
                    "platform": platform,
                    "external_id": url,
                    "fetched_at": (at + timedelta(days=8)).isoformat(),
                    "likes": 14,
                    "reposts": 3,
                    "replies": 2,
                    "quotes": 1,
                    "impressions": 1800,
                    "engagements": 60,
                    "link_clicks": 9,
                    "profile_visits": 11,
                    "source": "x-analytics",
                }
            )
    m.upsert("events", events)
    m.upsert("post_metrics", metrics_rows)
    accounts = []
    for i, day in enumerate(days[-35:]):
        accounts.append(
            {
                "platform": "x",
                "account": "herald_test",
                "date": day.isoformat(),
                "followers": 180 + i // 2,
                "following": 400,
                "posts": 2500 + i,
                "source": "manual",
            }
        )
        if i >= 10:
            accounts.append(
                {
                    "platform": "bluesky",
                    "account": "herald.test",
                    "date": day.isoformat(),
                    "followers": 5 + i,
                    "following": 30,
                    "posts": i,
                    "source": "api",
                }
            )
    m.upsert("account_metrics", accounts)
    m.close()


def seed_clips(store_path: Path, metrics_path: Path, now: datetime | None = None) -> None:
    """A clip registry for the dashboard's Clips panel: one redo clip that did well (re-record
    first), one fresh, one never posted, one whose last check failed, and one on hold."""
    from argus_herald import clips
    from argus_herald.metrics import Metrics

    now = now or datetime.now(UTC)
    store = Store(store_path)
    recorded = {"status": "posted", "project_version": "0.3.0", "demo_repo": "herald-test/argus-demos"}
    styles = clips.add(
        store,
        "atlas-styles",
        demo="atlas",
        project="argus-atlas",
        title="Atlas map styles",
        demo_commit="1" * 40,
        stale_if="the style picker moves",
        **recorded,
    )
    lens = clips.add(
        store, "lens-heatmap", demo="lens", project="argus-lens", title="Lens heatmap", demo_commit="2" * 40, **recorded
    )
    clips.add(store, "quarry-loop", demo="quarry", project="argus-quarry", title="Quarry licence check", format="loop")
    proof = clips.add(
        store,
        "proof-checkpoints",
        demo="proof",
        title="Proof checkpoints",
        status="recorded",
        project_version="0.1.0",
        demo_repo="herald-test/argus-demos",
        demo_commit="3" * 40,
    )
    clips.add(store, "pipeline-tour", title="Pipeline walkthrough", format="walkthrough", status="hold")

    def checked(clip: dict, state: str, ui_files: list[str], commits: int) -> None:
        store.save_freshness(
            clip["id"],
            state=state,
            commits=commits,
            ui_files=json.dumps(ui_files),
            files_changed=len(ui_files) + 2,
            head_commit="f" * 40,
            branch="main",
            newest_version=clip["project_version"],
            base_commit=clip["demo_commit"],
            base_version=clip["project_version"],
        )

    checked(styles, "redo", [f"atlas/src/styles/{n}.ts" for n in ("picker", "legend", "theme", "panel", "menu")], 6)
    checked(lens, "fresh", [], 0)
    checked(proof, "check", [], 2)
    store.freshness_failed("proof-checkpoints", "GitHub answered HTTP 502", base_commit="3" * 40, base_version="0.1.0")
    three_days = (now - timedelta(days=3)).isoformat()
    store.conn.execute("UPDATE clip_freshness SET checked_at = ? WHERE clip_id = 'proof-checkpoints'", (three_days,))

    # X posts from the last month: five plain ones are the baseline, two carry clips. They are 8 to
    # 20 days old: inside the 30-day baseline window and past min_age_hours (24), so all are rated.
    metrics = Metrics(metrics_path)
    posts = [
        (None, 100),
        (None, 120),
        (None, 90),
        (None, 110),
        (None, 100),
        ("atlas-styles", 950),
        ("lens-heatmap", 105),
    ]
    for n, (clip_id, impressions) in enumerate(posts):
        at = now - timedelta(days=20 - 2 * n)
        url = f"https://x.com/herald_test/status/19100000000000000{n:02d}"
        bid = store.log_broadcast(
            platform="twitter", url=url, kind="demo", project="", subject=f"Demo {n}", text="t", at=at.isoformat()
        )
        if clip_id:
            clips.link_broadcast(store, bid, clip_id)
        sample = {"platform": "twitter", "external_id": url, "fetched_at": (at + timedelta(days=2)).isoformat()}
        sample.update(likes=impressions // 20, reposts=1, replies=0, quotes=0, impressions=impressions)
        sample.update(engagements=None, link_clicks=None, profile_visits=None, source="api")
        metrics.upsert("post_metrics", [sample])
    metrics.close()
    store.conn.commit()
    store.close()


def bind_and_announce(sock: socket.socket, ready_file: str | None) -> str:
    """Bind and listen, then say so. A client that connects on the signal waits in the backlog
    until uvicorn accepts; before listen() it would be refused."""
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/"
    if ready_file:
        # Written whole, then moved into place: a polling reader never sees half a URL.
        partial = Path(f"{ready_file}.tmp")
        partial.write_text(url, encoding="utf-8")
        os.replace(partial, ready_file)
    return url


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", default="", help="Require this review token (ARGUS_HERALD_UI_TOKEN)")
    parser.add_argument("--ready-file", help="Write the URL here once listening")
    args = parser.parse_args()

    import uvicorn

    from argus_herald.ui.app import create_app

    root = Path(tempfile.mkdtemp(prefix="herald-bombadil-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    config = seed(root)
    seed_metrics(root / "metrics.sqlite")
    seed_clips(config.store_path, root / "metrics.sqlite")
    deny = root / "denylist.txt"
    deny.write_text(DENY_TERM + "\n", encoding="utf-8")
    deny.chmod(0o600)
    auditor = root / "auditor-denylist.txt"  # empty: the real auditor list is never read
    auditor.write_text("", encoding="utf-8")
    auditor.chmod(0o600)
    env = {
        "ARGUS_HERALD_UI_SECRET": "bombadil-secret",
        "ARGUS_HERALD_OPSEC_DENYLIST": str(deny),
        "AI_AUDIT_OPSEC_DENYLIST": str(auditor),
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/fixture",
        "TELEGRAM_BOT_TOKEN": "1:fixture",
        "TELEGRAM_CHAT_ID": "@fixture",
    }
    if args.token:
        env["ARGUS_HERALD_UI_TOKEN"] = args.token
    app = create_app(config, env, http=FakeHttp())

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    url = bind_and_announce(sock, args.ready_file)
    print(f"READY {url} (data in {root})", flush=True)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    server.run(sockets=[sock])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
