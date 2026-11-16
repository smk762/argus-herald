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
import shutil
import socket
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from PIL import Image  # noqa: E402

from pip_release.config import load_config  # noqa: E402
from pip_release.store import Store  # noqa: E402
from pip_release.workflow import attach_media  # noqa: E402

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
    store.add_draft(platform="discord", title="", body="Two weeks of Argus: atlas 0.3.0, lens 1.1.0.", link="",
                    kind="digest", post_key="digest:2026-11-16", subject="Digest 16 Nov")
    store.add_draft(platform="telegram", title="", body="Argus Lens 1.1.0 adds heatmaps.", link="",
                    kind="headline", post_key="headline:argus-lens==1.1.0", subject="Lens 1.1.0")
    store.conn.commit()
    store.close()
    return config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", default="", help="Require this review token (PIP_RELEASE_UI_TOKEN)")
    parser.add_argument("--ready-file", help="Write the URL here once listening")
    args = parser.parse_args()

    import uvicorn

    from pip_release.ui.app import create_app

    root = Path(tempfile.mkdtemp(prefix="herald-bombadil-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    config = seed(root)
    deny = root / "denylist.txt"
    deny.write_text(DENY_TERM + "\n", encoding="utf-8")
    deny.chmod(0o600)
    env = {
        "PIP_RELEASE_UI_SECRET": "bombadil-secret",
        "PIP_RELEASE_OPSEC_DENYLIST": str(deny),
        "AI_AUDIT_OPSEC_DENYLIST": str(root / "no-auditor-list.txt"),
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/fixture",
        "TELEGRAM_BOT_TOKEN": "1:fixture",
        "TELEGRAM_CHAT_ID": "@fixture",
    }
    if args.token:
        env["PIP_RELEASE_UI_TOKEN"] = args.token
    app = create_app(config, env, http=FakeHttp())

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/"
    if args.ready_file:
        Path(args.ready_file).write_text(url, encoding="utf-8")
    print(f"READY {url} (data in {root})", flush=True)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    server.run(sockets=[sock])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
