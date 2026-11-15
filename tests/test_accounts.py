import json

import pytest

from pip_release.config import ConfigError, load_config
from pip_release.metrics import Metrics, collect
from pip_release.store import Store

CHANNEL = "UC" + "b" * 22


class Fake:
    def __init__(self, bluesky=200, mastodon=200):
        self.status = {"bluesky": bluesky, "mastodon": mastodon}
        self.calls = []

    def request(self, method, url, body=None, headers=None):
        self.calls.append(url)
        j = lambda data: (200, json.dumps(data).encode())  # noqa: E731
        if "app.bsky.actor.getProfile" in url:
            if self.status["bluesky"] != 200:
                return self.status["bluesky"], b"{}"
            return j({"handle": "dragonhound.dev", "followersCount": 12, "followsCount": 30, "postsCount": 4})
        if "/api/v1/accounts/lookup" in url:
            if self.status["mastodon"] != 200:
                return self.status["mastodon"], b"{}"
            return j({"followers_count": 7, "following_count": 9, "statuses_count": 3})
        if "youtube/v3/channels" in url:
            return j({"items": [{"statistics": {"subscriberCount": "41", "videoCount": "5"}}]})
        return j({"data": []}) if "pypistats" in url else (404, b"{}")


def _config(tmp_path, accounts):
    path = tmp_path / "c.yaml"
    path.write_text("pypi: {projects: []}\n"
                    "destinations:\n  twitter: {enabled: true, username: dragonhound3}\n"
                    f"sources:\n  youtube: {{channel_id: {CHANNEL}}}\n" + accounts, encoding="utf-8")
    cfg = load_config(path)
    cfg.store_path = tmp_path / "s.sqlite"
    return cfg


def test_accounts_config(tmp_path):
    cfg = _config(tmp_path, "accounts: {bluesky: '@dragonhound.dev', mastodon: '@dragonhound@mapstodon.space'}\n")
    assert cfg.accounts.bluesky == "dragonhound.dev" and cfg.accounts.youtube == CHANNEL and cfg.accounts.x == "dragonhound3"
    assert cfg.accounts.mastodon_parts == ("https://mapstodon.space", "dragonhound")
    for bad in ("accounts: {threads: x}\n", "accounts: {bluesky: 'not a handle'}\n",
                "accounts: {mastodon: dragonhound}\n", "accounts: {youtube: nope}\n"):
        with pytest.raises(ConfigError):
            _config(tmp_path, bad)


def test_collect_counts_and_skips_missing_accounts(tmp_path):
    cfg = _config(tmp_path, "accounts: {bluesky: dragonhound.dev, mastodon: '@dragonhound@mapstodon.space'}\n")
    store, metrics = Store(cfg.store_path), Metrics(tmp_path / "m.sqlite")
    report = collect(cfg, store, metrics, Fake(), {"YOUTUBE_API_KEY": "k"})
    rows = {r["platform"]: tuple(r)[3:6] for r in metrics.conn.execute("SELECT * FROM account_metrics")}
    assert rows == {"bluesky": (12, 30, 4), "mastodon": (7, 9, 3), "youtube": (41, None, 5)}
    assert report.errors == []

    metrics2 = Metrics(tmp_path / "m2.sqlite")
    report = collect(cfg, store, metrics2, Fake(bluesky=400, mastodon=404), {})
    assert "bluesky account dragonhound.dev: not found (yet)" in report.skipped
    assert "mastodon account @dragonhound@mapstodon.space: not found (yet)" in report.skipped
    assert "YouTube subscribers: YOUTUBE_API_KEY is not set" in report.skipped
    assert report.errors == [] and metrics2.count("account_metrics") == 0


def test_import_x_and_followers_report(tmp_path, capsys):
    from pip_release.cli import main

    cfg = tmp_path / "c.yaml"
    cfg.write_text("pypi: {projects: [demo]}\ndestinations:\n  twitter: {enabled: true, username: dragonhound3}\n",
                   encoding="utf-8")
    base = ["--config", str(cfg), "--env", str(tmp_path / "no.env"), "--store", str(tmp_path / "s.sqlite")]
    assert main([*base, "followers"]) == 0 and "No metrics yet" in capsys.readouterr().out
    assert main([*base, "import", "x-account", "--date", "2026-11-15", "--followers", "180", "--following", "407",
                 "--posts", "2573"]) == 0
    assert main([*base, "import", "x-account", "--date", "2026-11-22", "--followers", "196"]) == 0
    assert main([*base, "import", "x-account", "--date", "nope", "--followers", "1"]) == 2
    capsys.readouterr()
    assert main([*base, "followers"]) == 0
    assert "x         dragonhound3                 2026-11-22  196 followers  +16 in 7 days" in capsys.readouterr().out
