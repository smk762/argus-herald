import json

from argus_herald.config import load_config
from argus_herald.metrics import Metrics, collect
from argus_herald.store import Store


class Fake:
    def __init__(self, stars=3):
        self.stars = stars
        self.calls = []

    def request(self, method, url, body=None, headers=None):
        self.calls.append((url, dict(headers or {})))
        j = lambda data: (200, json.dumps(data).encode())  # noqa: E731
        if "/users/smk762/repos" in url:
            return j([{"name": "argus-atlas", "stargazers_count": 3}, {"name": "other", "stargazers_count": 8}])
        if url.endswith("/traffic/views?per=day"):
            return j(
                {
                    "views": [
                        {"timestamp": "2026-11-14T00:00:00Z", "count": 5, "uniques": 2},
                        {"timestamp": "2026-11-15T00:00:00Z", "count": 7, "uniques": 3},
                    ]
                }
            )
        if url.endswith("/traffic/clones?per=day"):
            return j({"clones": [{"timestamp": "2026-11-15T00:00:00Z", "count": 4, "uniques": 1}]})
        if url.endswith("/popular/referrers"):
            return j(
                [{"referrer": "t.co", "count": 9, "uniques": 4}, {"referrer": "bsky.app", "count": 2, "uniques": 2}]
            )
        if url.endswith("/popular/paths"):
            return j([{"path": "/smk762/argus-atlas", "title": "atlas", "count": 9, "uniques": 4}])
        if "/stargazers" in url:
            page = int(url.rsplit("page=", 1)[1])
            items = [
                {"starred_at": f"2026-11-{10 + i:02d}T00:00:00Z", "user": {"login": f"u{i}"}} for i in range(self.stars)
            ]
            return j(items if page == 1 else [])
        if url.startswith("https://pypistats.org/api/packages/argus-atlas/overall"):
            return j(
                {
                    "data": [
                        {"category": "without_mirrors", "date": "2026-11-14", "downloads": 10},
                        {"category": "with_mirrors", "date": "2026-11-14", "downloads": 99},
                        {"category": "without_mirrors", "date": "2026-11-15", "downloads": 12},
                    ]
                }
            )
        if url == "https://pypi.org/pypi/argus-atlas/json":
            return j(
                {
                    "info": {"version": "0.2.0"},
                    "releases": {
                        "0.1.0": [{"yanked": False, "upload_time_iso_8601": "2026-11-01T00:00:00Z"}],
                        "0.2.0": [{"yanked": False, "upload_time_iso_8601": "2026-11-14T08:00:00Z"}],
                    },
                }
            )
        return 404, b"{}"


def _setup(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        "pypi:\n  projects: [argus-atlas]\n  discover: {github_owner: smk762, prefix: argus-}\n"
        "destinations:\n  twitter: {enabled: true}\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    cfg.store_path = tmp_path / "s.sqlite"
    store = Store(cfg.store_path)
    draft_id, _ = store.add_draft(
        project="argus-atlas", version="0.2.0", platform="twitter", title="", body="hi", link="l"
    )
    store.conn.execute("UPDATE drafts SET status = 'handed_off' WHERE id = ?", (draft_id,))
    from argus_herald.workflow import confirm

    confirm(store, draft_id, "https://x.com/dragonhound3/status/5")
    return cfg, store, Metrics(tmp_path / "metrics.sqlite")


def test_collect_everything_with_a_token_and_it_is_idempotent(tmp_path):
    cfg, store, metrics = _setup(tmp_path)
    http = Fake()
    report = collect(cfg, store, metrics, http, {"GITHUB_TOKEN": "t"})
    assert report.errors == [] and report.skipped == []
    q = lambda sql: [tuple(r) for r in metrics.conn.execute(sql)]  # noqa: E731
    assert q("SELECT date, views, uniques, clones, clone_uniques FROM gh_traffic ORDER BY date") == [
        ("2026-11-14", 5, 2, 0, 0),
        ("2026-11-15", 7, 3, 4, 1),
    ]
    assert q("SELECT referrer, uniques FROM gh_referrers ORDER BY referrer") == [("bsky.app", 2), ("t.co", 4)]
    assert q("SELECT COUNT(*) FROM gh_stars") == [(3,)]
    assert "u0" not in str(q("SELECT user_hash FROM gh_stars"))  # logins aren't stored
    assert q("SELECT date, downloads FROM pypi_downloads ORDER BY date") == [("2026-11-14", 10), ("2026-11-15", 12)]
    assert q("SELECT kind, source, version, url FROM events ORDER BY kind, version") == [
        ("broadcast", "twitter", "0.2.0", "https://x.com/dragonhound3/status/5"),
        ("release", "pypi", "0.1.0", "https://pypi.org/project/argus-atlas/0.1.0/"),
        ("release", "pypi", "0.2.0", "https://pypi.org/project/argus-atlas/0.2.0/"),
    ]
    assert all(h.get("Authorization") == "Bearer t" for u, h in http.calls if "api.github.com" in u)
    assert not any("/repos/smk762/other/" in u for u, _ in http.calls)

    http.stars = 4
    collect(cfg, store, metrics, http, {"GITHUB_TOKEN": "t"})
    assert q("SELECT COUNT(*) FROM gh_traffic") == [(2,)] and q("SELECT COUNT(*) FROM events") == [(3,)]
    assert q("SELECT COUNT(*) FROM gh_stars") == [(4,)]


def test_without_a_token_traffic_is_skipped_and_the_rest_runs(tmp_path):
    cfg, store, metrics = _setup(tmp_path)
    http = Fake()
    report = collect(cfg, store, metrics, http, {})
    assert report.skipped == ["GitHub traffic, referrers, paths and star history: GITHUB_TOKEN is not set"]
    assert not any("/traffic/" in u or "/stargazers" in u for u, _ in http.calls)
    assert metrics.count("gh_stars") == 0 and metrics.count("pypi_downloads") == 2
    assert [tuple(r) for r in metrics.conn.execute("SELECT repo, stars FROM gh_star_counts")] == [("argus-atlas", 3)]


def test_cli_collect_reports_errors(tmp_path, capsys, monkeypatch):
    from argus_herald import cli

    cfg, store, metrics = _setup(tmp_path)
    store.close()
    metrics.close()

    class Broken(Fake):
        def request(self, method, url, body=None, headers=None):
            if "pypistats" in url:
                return 429, b""
            return super().request(method, url, body, headers)

    monkeypatch.setenv("GITHUB_TOKEN", "")
    store = Store(cfg.store_path)
    assert cli.cmd_collect(cfg, store, Broken(), None) == 1
    err = capsys.readouterr().err
    assert "error pypi_downloads" in err and "skipped GitHub traffic" in err
    assert (tmp_path / "metrics.sqlite").exists()
