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


class Stargazers:
    """GitHub's list, which shrinks when someone unstars."""

    def __init__(self, logins):
        self.logins = list(logins)
        self.pages = []

    def request(self, method, url, body=None, headers=None):
        page = int(url.rsplit("page=", 1)[1])
        self.pages.append(page)
        items = [{"starred_at": "2026-11-10T00:00:00Z", "user": {"login": login}} for login in self.logins]
        return 200, json.dumps(items[(page - 1) * 100 : page * 100]).encode()


def test_new_stars_are_found_after_unstars(tmp_path):
    from argus_herald.metrics import collect_stars

    m = Metrics(tmp_path / "m.sqlite")
    http = Stargazers(f"u{i}" for i in range(200))
    collect_stars(m, http, "smk762", ["argus-atlas"], "t")
    assert m.count("gh_stars", repo="argus-atlas") == 200
    # One unstar, one new star: the new one is at index 199, on page 2, not page 3.
    http.logins = [f"u{i}" for i in range(200) if i != 5] + ["new-a"]
    assert "(1 new)" in collect_stars(m, http, "smk762", ["argus-atlas"], "t")
    # Many unstars: the list is two pages shorter than the local count says.
    http.logins = [f"u{i}" for i in range(40)] + ["new-b", "new-c"]
    http.pages.clear()
    assert "(2 new)" in collect_stars(m, http, "smk762", ["argus-atlas"], "t")
    assert http.pages == [2, 1]
    # A steady list is read from the page the known stars end on.
    m2 = Metrics(tmp_path / "m2.sqlite")
    steady = Stargazers(f"u{i}" for i in range(250))
    collect_stars(m2, steady, "smk762", ["argus-atlas"], "t")
    steady.pages.clear()
    assert "(0 new)" in collect_stars(m2, steady, "smk762", ["argus-atlas"], "t")
    assert steady.pages == [2, 3]


def test_stars_past_the_fiftieth_page_are_recorded(tmp_path):
    from argus_herald.metrics import collect_stars

    m = Metrics(tmp_path / "m.sqlite")
    http = Stargazers(f"u{i}" for i in range(5150))
    collect_stars(m, http, "smk762", ["argus-atlas"], "t")
    assert m.count("gh_stars", repo="argus-atlas") == 5150
    http.logins.append("new-a")
    assert "(1 new)" in collect_stars(m, http, "smk762", ["argus-atlas"], "t")


def test_a_repo_with_more_stars_than_github_lists_says_so(tmp_path, monkeypatch):
    import argus_herald.metrics as metrics_module

    monkeypatch.setattr(metrics_module, "STAR_PAGES", 2)
    m = Metrics(tmp_path / "m.sqlite")
    detail = metrics_module.collect_stars(m, Stargazers(f"u{i}" for i in range(250)), "smk762", ["argus-atlas"], "t")
    assert m.count("gh_stars") == 200
    assert "first 200 stargazers" in detail and "argus-atlas" in detail
    m2 = Metrics(tmp_path / "m2.sqlite")
    assert "stargazers" not in metrics_module.collect_stars(m2, Stargazers(["u1"]), "smk762", ["argus-atlas"], "t")


def test_an_upsert_keeps_the_columns_it_does_not_carry(tmp_path):
    m = Metrics(tmp_path / "m.sqlite")
    key = {"platform": "bluesky", "external_id": "at://x/1", "fetched_at": "2026-11-10T00:00:00+00:00"}
    m.upsert("post_metrics", [{**key, "likes": 3, "impressions": 90}])
    m.upsert("post_metrics", [{**key, "likes": 5}])
    row = m.conn.execute("SELECT likes, impressions FROM post_metrics").fetchone()
    assert (row["likes"], row["impressions"]) == (5, 90)
    assert m.count("post_metrics") == 1


def test_upsert_and_count_only_take_names_the_schema_has(tmp_path):
    import pytest

    m = Metrics(tmp_path / "m.sqlite")
    row = {"repo": "argus-atlas", "user_hash": "h", "starred_at": "2026-11-10T00:00:00Z"}
    with pytest.raises(ValueError, match="no table"):
        m.upsert("gh_stars; DROP TABLE events", [row])
    with pytest.raises(ValueError, match="no column"):
        m.upsert("gh_stars", [{**row, "starred_at) VALUES (1); --": 1}])
    with pytest.raises(ValueError, match="no column"):
        m.count("gh_stars", **{"1 OR 1": 1})
    with pytest.raises(ValueError, match="same columns"):
        m.upsert("gh_stars", [row, {"repo": "argus-atlas", "user_hash": "h2"}])
    assert m.count("gh_stars") == 0 and m.count("events") == 0


def test_an_interrupted_transaction_is_rolled_back(tmp_path):
    import pytest

    m = Metrics(tmp_path / "m.sqlite")
    row = {"repo": "argus-atlas", "user_hash": "h", "starred_at": "2026-11-10T00:00:00Z"}
    for interrupt in (KeyboardInterrupt, SystemExit, GeneratorExit):
        with pytest.raises(interrupt), m.transaction():
            m.conn.execute(
                "INSERT INTO gh_stars (repo, user_hash, starred_at) VALUES (:repo, :user_hash, :starred_at)", row
            )
            raise interrupt
        assert not m.conn.in_transaction and m.count("gh_stars") == 0
    assert m.upsert("gh_stars", [row]) and m.count("gh_stars") == 1  # the connection still works


def test_a_repo_the_token_cannot_read_does_not_cost_the_others_their_traffic(tmp_path):
    cfg, store, metrics = _setup(tmp_path)

    class Partial(Fake):
        def request(self, method, url, body=None, headers=None):
            if "/users/smk762/repos" in url:
                names = ("argus-atlas", "argus-herald", "argus-lens")
                return 200, json.dumps([{"name": n, "stargazers_count": 1} for n in names]).encode()
            if "/repos/smk762/argus-herald/traffic/" in url:
                self.calls.append((url, dict(headers or {})))
                return 403, b'{"message": "Resource not accessible by personal access token"}'
            return super().request(method, url, body, headers)

    http = Partial()
    report = collect(cfg, store, metrics, http, {"GITHUB_TOKEN": "t"})
    repos = [r[0] for r in metrics.conn.execute("SELECT DISTINCT repo FROM gh_traffic ORDER BY repo")]
    assert repos == ["argus-atlas", "argus-lens"]  # the repo after the refused one is still collected
    assert any(line.startswith("gh_traffic: 2 repos") for line in report.lines)
    assert [e for e in report.errors if e.startswith("gh_traffic")] == [
        "gh_traffic: the token can't read traffic for argus-herald. Add them to the token's repositories; "
        "GitHub keeps traffic for 14 days only"
    ]
    assert sum("/argus-herald/traffic/" in url for url, _ in http.calls) == 1  # asked once, then left alone


def test_traffic_refused_for_every_repo_is_an_error(tmp_path):
    cfg, store, metrics = _setup(tmp_path)

    class Denied(Fake):
        def request(self, method, url, body=None, headers=None):
            if "/traffic/" in url:
                return 403, b"{}"
            return super().request(method, url, body, headers)

    report = collect(cfg, store, metrics, Denied(), {"GITHUB_TOKEN": "t"})
    assert any(e.startswith("gh_traffic: GET") and "403" in e for e in report.errors)
    assert metrics.count("gh_traffic") == 0
