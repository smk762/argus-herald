import json

import pytest

from pip_release.metrics import Metrics, collect_events
from pip_release.store import Store
from pip_release.workflow import ReviewError, edit_logged, log_broadcast


class NoPyPI:
    def request(self, method, url, body=None, headers=None):
        return 404, b"{}"


def test_log_validate_edit_delete(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    rid = log_broadcast(store, platform="Reddit", url="https://www.reddit.com/r/gis/comments/abc/", kind="demo",
                        project="argus-atlas", subject="r/gis atlas", text="", at="2026-12-15T09:30:00+05:00")
    row = store.logged_broadcast(rid)
    assert (row["platform"], row["at"]) == ("reddit", "2026-12-15T04:30:00+00:00")
    x = log_broadcast(store, platform="x", url="https://twitter.com/dragonhound3/status/9?s=20", at="2026-12-16")
    assert store.logged_broadcast(x)["url"] == "https://x.com/dragonhound3/status/9"
    assert store.logged_broadcast(x)["platform"] == "twitter"
    for bad in (dict(platform="Re ddit", url="https://a.org"), dict(platform="hn", url="http://a.org"),
                dict(platform="hn", url="https://a.org", kind="tweet"), dict(platform="hn", url="https://a.org", at="soon"),
                dict(platform="reddit", url="https://www.reddit.com/r/gis/comments/abc/")):
        with pytest.raises(ReviewError):
            log_broadcast(store, **bad)
    edit_logged(store, rid, url="https://www.reddit.com/r/gis/comments/xyz/")
    assert store.logged_broadcast(rid)["url"].endswith("/xyz/") and store.logged_broadcast(rid)["kind"] == "demo"
    with pytest.raises(ReviewError):
        edit_logged(store, 999, url="https://a.org")
    assert store.delete_logged(rid) and not store.delete_logged(rid)
    store.close()


def test_events_follow_edits_and_deletes(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    metrics = Metrics(tmp_path / "m.sqlite")
    rid = log_broadcast(store, platform="hn", url="https://news.ycombinator.com/item?id=1", kind="demo", at="2027-01-05")
    collect_events(metrics, store, NoPyPI(), [])
    rows = [dict(r) for r in metrics.conn.execute("SELECT * FROM events")]
    assert [(r["source"], r["url"]) for r in rows] == [("hn", "https://news.ycombinator.com/item?id=1")]
    assert json.loads(rows[0]["detail"])["logged"] == rid
    edit_logged(store, rid, url="https://news.ycombinator.com/item?id=2")
    collect_events(metrics, store, NoPyPI(), [])
    assert [r["url"] for r in metrics.conn.execute("SELECT url FROM events")] == ["https://news.ycombinator.com/item?id=2"]
    store.delete_logged(rid)
    collect_events(metrics, store, NoPyPI(), [])
    assert metrics.count("events") == 0


def test_cli_log_list_edit_delete(tmp_path, capsys):
    from pip_release.cli import main

    cfg = tmp_path / "c.json"
    cfg.write_text('{"pypi":{"projects":["demo"]},"destinations":{"patreon":{"enabled":true}}}', encoding="utf-8")
    base = ["--config", str(cfg), "--env", str(tmp_path / "no.env"), "--store", str(tmp_path / "s.sqlite")]
    assert main([*base, "log-broadcast", "--platform", "youtube", "--url", "https://www.youtube.com/watch?v=AAAAAAAAAAA",
                 "--kind", "video", "--subject", "Atlas demo", "--at", "2026-12-01T10:00:00Z"]) == 0
    assert "Logged #1: youtube" in capsys.readouterr().out
    assert main([*base, "broadcasts"]) == 0
    assert "youtube      video     https://www.youtube.com/watch?v=AAAAAAAAAAA  Atlas demo" in capsys.readouterr().out
    assert main([*base, "log-broadcast", "--id", "1", "--subject", "Atlas coastline"]) == 0
    assert main([*base, "log-broadcast", "--platform", "hn"]) == 2
    assert main([*base, "broadcasts", "delete", "1"]) == 0
    assert main([*base, "broadcasts", "delete", "1"]) == 2


def test_stargazer_denial_is_a_skip_not_an_error(tmp_path):
    from test_metrics import Fake, _setup
    from pip_release.metrics import collect

    class Denied(Fake):
        def request(self, method, url, body=None, headers=None):
            if "/stargazers" in url:
                return 403, b"{}"
            return super().request(method, url, body, headers)

    cfg, store, metrics = _setup(tmp_path)
    report = collect(cfg, store, metrics, Denied(), {"GITHUB_TOKEN": "t"})
    assert report.errors == []
    assert any(s.startswith("GitHub star history: HTTP 403") for s in report.skipped)
    assert metrics.count("gh_traffic") == 2
