import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from argus_herald import clips, freshness
from argus_herald.cli import main
from argus_herald.config import DEFAULT_IGNORE_PATHS, DEFAULT_UI_PATHS, ConfigError, load_config
from argus_herald.github import Comparison, compare
from argus_herald.http import HttpError
from argus_herald.store import Store
from argus_herald.workflow import ReviewError

BASE = "16e992da7b"
HEAD = "c" * 40
API = "https://api.github.com/repos"


def compared(ahead, files=(), *, head=HEAD, renamed=(), commits=None):
    """A compare API body: `ahead` commits ending at `head`, these files changed."""
    listed = [{"sha": "b" * 40}] * (max(ahead, 1) - 1) + [{"sha": head}]
    return {
        "status": "ahead" if ahead else "identical",
        "ahead_by": ahead,
        "behind_by": 0,
        "total_commits": ahead,
        "commits": (listed if commits is None else commits) if ahead else [],
        "merge_base_commit": {"sha": head if not ahead else BASE + "0" * 30},
        "files": [{"filename": f} for f in files] + [{"filename": n, "previous_filename": o} for o, n in renamed],
    }


class GitHub:
    """Answers compare calls from a table; anything else is a 404. No network."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.calls = []

    def request(self, method, url, body=None, headers=None):
        self.calls.append((url, dict(headers or {})))
        answer = self.answers.get(url.removeprefix(API + "/"))
        if answer is None:
            return 404, b"{}"
        if isinstance(answer, int):
            return answer, b"{}"
        if isinstance(answer, str):
            return 200, answer.encode()
        return 200, json.dumps(answer).encode()


def config(tmp_path, text=""):
    path = tmp_path / "c.yaml"
    path.write_text("pypi:\n  projects: [argus-atlas]\n" + text, encoding="utf-8")
    cfg = load_config(path)
    cfg.store_path = tmp_path / "s.sqlite"
    return cfg


def recorded(store, clip_id, **fields):
    defaults = {
        "demo": "atlas",
        "project": "argus-atlas",
        "status": "recorded",
        "project_version": "0.4.0",
        "demo_repo": "smk762/argus-realms",
        "demo_commit": BASE,
    }
    return clips.add(store, clip_id, **{**defaults, **fields})


# ---- pure parts ----------------------------------------------------------------------------


def test_ui_path_globs():
    cases = [
        ("src/**", "src/app.tsx", True),
        ("src/**", "src/map/layers/coast.ts", True),
        ("src/**", "lib/src/app.ts", False),  # a pattern with a slash is anchored at the root
        ("src/**", "srcs/app.ts", False),
        ("*.css", "style.css", True),
        ("*.css", "public/themes/dark.css", True),  # no slash: the file name at any depth
        ("*.css", "style.css.map", False),
        ("docs/*.md", "docs/intro.md", True),
        ("docs/*.md", "docs/guide/intro.md", False),  # one star stays in its directory
        ("**/test_?.py", "a/b/test_x.py", True),
        ("**/test_?.py", "test_x.py", True),
        ("/frontend/**", "frontend/index.html", True),
        ("a+b/[x].js", "a+b/[x].js", True),  # regex characters are literal
        ("**/tests/**", "tests/x.js", True),  # a leading **/ also matches at the root
        ("**/tests/**", "a/b/tests/c/x.js", True),
        ("**/tests/**", "contests/x.js", False),
        ("**/tests/**", "tests.js", False),
    ]
    for pattern, path, expected in cases:
        assert freshness.matches(path, pattern) is expected, (pattern, path)
    assert freshness.ui_files(["README.md", "src/a.ts", "data/x.json", "web/x.html"], ("src/**", "*.html")) == [
        "src/a.ts",
        "web/x.html",
    ]


def test_versions():
    assert freshness.newest_version(["0.4.0", "0.10.1", "0.11.0rc1", "junk"]) == "0.10.1"
    assert freshness.newest_version(["0.1.0rc1"]) == "0.1.0rc1"
    assert freshness.newest_version([]) == ""
    assert freshness.minor_moved("0.4.0", "0.5.0") and not freshness.minor_moved("0.4.0", "0.4.9")
    assert not freshness.minor_moved("0.4.0", "") and not freshness.minor_moved("", "0.5.0")


def test_classify_follows_the_table():
    ui = ("src/**", "*.css")
    same = Comparison(0, HEAD, (), False)
    docs = Comparison(3, HEAD, ("README.md", "data/fixtures.json"), False)
    styled = Comparison(2, HEAD, ("docs/x.md", "public/app.css"), False)
    assert freshness.classify(same, "0.4.0", "0.4.0", ui).state == "fresh"
    assert freshness.classify(same, "0.4.0", "0.4.3", ui).state == "fresh"  # a patch doesn't count
    assert freshness.classify(same, "0.4.0", "0.5.0", ui).state == "check"  # the minor version moved
    assert freshness.classify(docs, "0.4.0", "0.4.0", ui).state == "check"  # changed, but not the UI
    verdict = freshness.classify(styled, "0.4.0", "0.4.0", ui)
    assert (verdict.state, verdict.ui_files) == ("redo", ("public/app.css",))
    assert freshness.classify(None, "0.4.0", "0.4.0", ui).state == "unknown"
    assert freshness.classify(None, "0.4.0", "0.6.0", ui).state == "check"


def by_default(*files):
    return freshness.classify(
        Comparison(1, HEAD, files, False), "0.4.0", "0.4.0", DEFAULT_UI_PATHS, DEFAULT_IGNORE_PATHS
    )


def test_backend_only_change_asks_for_a_check_not_a_redo():
    verdict = by_default("src/pkg/api.py", "src/pkg/db.py", "src/pkg/cli.py")
    assert (verdict.state, verdict.ui_files) == ("check", ())


def test_front_end_files_ask_for_a_redo_by_default():
    for path in ("src/pkg/static/app.js", "frontend/src/App.tsx", "templates/index.html", "site/styles/main.scss"):
        verdict = by_default("src/pkg/api.py", path)
        assert (verdict.state, verdict.ui_files) == ("redo", (path,)), path


def test_default_ignore_paths():
    ignored = [
        *(f"{d}/x.js" for d in ("tests", "test", "__tests__", "docs", "scripts", "e2e")),  # top level
        *(f"frontend/src/{d}/a.tsx" for d in ("tests", "test", "__tests__", "docs", "scripts", "e2e")),  # nested
        "tests/ui/x.js",
        "frontend/src/__tests__/a.tsx",
        "a.test.ts",
        "web/a.test.tsx",
        "a.spec.js",
        "vite.config.ts",
        "web/tailwind.config.mjs",
        "types.d.ts",
        "src/types/env.d.ts",
    ]
    for path in ignored:
        assert by_default(path).state == "check", path
    near_misses = [
        "src/contest/app.js",  # "test" inside a directory name
        "src/latest.js",
        "src/tests.js",  # a file, not the tests directory
        "mytests/app.js",
        "configure.js",
        "src/testing/app.ts",
        "spec.js",
        "docsite/app.js",
        "ad.ts",
    ]
    for path in near_misses:
        assert by_default(path).state == "redo", path
    assert freshness.ui_files(["tests/x.js", "app.js"], ("*.js",), ()) == ["tests/x.js", "app.js"]  # nothing ignored


# ---- the GitHub call ---------------------------------------------------------------------------


def test_compare_reads_files_head_and_auth():
    url = f"smk762/argus-realms/compare/{BASE}...dev"
    gh = GitHub({url: compared(2, ["src/a.ts"], renamed=[("old.css", "src/new.css")])})
    result = compare(gh, "smk762", "argus-realms", BASE, "dev")
    assert result == Comparison(2, HEAD, ("src/a.ts", "src/new.css", "old.css"), False)
    assert "Authorization" not in gh.calls[0][1]  # public repos need no token
    compare(gh, "smk762", "argus-realms", BASE, "dev", token="t")
    assert gh.calls[-1][1]["Authorization"] == "Bearer t"

    # Nothing new: the head is the merge base.
    gh.answers[url] = compared(0)
    assert compare(gh, "smk762", "argus-realms", BASE, "dev") == Comparison(0, HEAD, (), False)

    # More commits than GitHub lists: the tip is asked for separately.
    gh.answers[url] = compared(400, [f"data/{i}.json" for i in range(300)], commits=[{"sha": "b" * 40}] * 250)
    gh.answers["smk762/argus-realms/commits/dev"] = "d" * 40 + "\n"
    result = compare(gh, "smk762", "argus-realms", BASE, "dev")
    assert (result.ahead_by, result.head_commit, result.truncated) == (400, "d" * 40, True)
    assert gh.calls[-1][1]["Accept"] == "application/vnd.github.sha"

    for answer in (404, 403, {"message": "x"}):
        gh.answers[url] = answer
        with pytest.raises((HttpError, ValueError)):
            compare(gh, "smk762", "argus-realms", BASE, "dev")


# ---- refresh, the store, ack -------------------------------------------------------------------


def test_refresh_states_and_reasons(tmp_path):
    cfg = config(tmp_path, "clips:\n  owner: smk762\n")
    store = Store(cfg.store_path)
    store.baseline("argus-atlas", ["0.4.0", "0.4.2"])
    store.baseline("argus_Lens", ["0.3.0", "0.4.1"])  # matched by PyPI's name rules
    recorded(store, "fresh-one")
    recorded(store, "fresh-two")  # same repo and commit: one call
    recorded(store, "docs", demo_commit="aaaaaaa")
    recorded(store, "ui", demo_commit="bbbbbbb", stale_if="the coastline style changes")
    recorded(store, "lens", project="argus-lens", project_version="0.3.0", demo_repo="argus-lens")  # bare name
    recorded(store, "nowhere", demo_repo="")
    recorded(store, "parked", status="stale")  # not checked
    gh = GitHub(
        {
            f"smk762/argus-realms/compare/{BASE}...dev": compared(0),
            "smk762/argus-realms/compare/aaaaaaa...dev": compared(3, ["README.md", "data/fixtures.json"]),
            "smk762/argus-realms/compare/bbbbbbb...dev": compared(
                12, ["src/a.ts", "src/b.ts", "public/app.css", "index.html", "README.md"]
            ),
            f"smk762/argus-lens/compare/{BASE}...dev": compared(0),
        }
    )
    summary = freshness.refresh(store, gh, cfg)
    assert summary.errors == [] and summary.line == "6 clips: 1 redo, 2 check, 1 unknown, 2 fresh"
    assert len(gh.calls) == 4
    rows = store.clip_freshness()
    assert {cid: rows[cid]["state"] for cid in rows} == {
        "fresh-one": "fresh",
        "fresh-two": "fresh",
        "docs": "check",
        "ui": "redo",
        "lens": "check",  # 0.3.0 -> 0.4.1: the minor version moved
        "nowhere": "unknown",
    }
    assert rows["ui"]["newest_version"] == "0.4.2" and rows["ui"]["head_commit"] == HEAD

    ui = freshness.explain(store.clip("ui"), rows["ui"])
    assert ui[0] == ("12 commits on dev since bbbbbbb; 4 UI files changed: src/a.ts, src/b.ts, public/app.css +1 more.")
    assert "Stale if: the coastline style changes" in ui
    assert freshness.explain(store.clip("docs"), rows["docs"])[0] == (
        "3 commits on dev since aaaaaaa; 2 files changed, none under the UI paths."
    )
    assert "argus-lens 0.3.0 -> 0.4.1 (minor version moved)." in freshness.explain(store.clip("lens"), rows["lens"])
    assert "argus-atlas 0.4.0 -> 0.4.2 (patch only)." in freshness.explain(store.clip("fresh-one"), rows["fresh-one"])
    assert freshness.explain(store.clip("nowhere"), rows["nowhere"])[0] == (
        "Demo source not compared: no demo_repo recorded."
    )
    store.close()


def test_owner_resolution_and_per_demo_override(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    recorded(store, "bare", demo_repo="argus-realms")
    recorded(store, "full", demo="lens", demo_repo="other-org/argus-lens")

    # No owner anywhere: a bare name can't be compared, which is said, not raised.
    cfg = config(tmp_path)
    gh = GitHub({f"other-org/argus-lens/compare/{BASE}...dev": compared(0)})
    freshness.refresh(store, gh, cfg)
    rows = store.clip_freshness()
    assert rows["bare"]["state"] == "unknown" and "has no owner" in rows["bare"]["reason"]
    assert rows["full"]["state"] == "fresh"

    # Fallbacks: github.owner, then pypi.discover.github_owner; clips.owner wins over both.
    assert freshness.locate(store.clip("bare"), config(tmp_path, "  discover: {github_owner: d}\n")) == (
        "d",
        "argus-realms",
    )
    both = "  discover: {github_owner: d}\ngithub: {owner: g, prefix: argus-}\n"
    assert freshness.locate(store.clip("bare"), config(tmp_path, both))[0] == "g"
    assert freshness.locate(store.clip("bare"), config(tmp_path, both + "clips: {owner: c}\n"))[0] == "c"
    url_form = {**store.clip("bare"), "demo_repo": "https://github.com/x/argus-realms.git"}
    assert freshness.locate(url_form, cfg) == ("x", "argus-realms")
    assert "isn't a GitHub repo" in freshness.locate({**url_form, "demo_repo": "a/b/c"}, cfg)

    # A demo can deploy from another branch and keep its UI elsewhere.
    cfg = config(
        tmp_path,
        "clips:\n  owner: smk762\n  branch: main\n  demos:\n    lens: {branch: release, ui_paths: [web/**]}\n",
    )
    gh = GitHub(
        {
            f"smk762/argus-realms/compare/{BASE}...main": compared(1, ["web/app.py"]),
            f"other-org/argus-lens/compare/{BASE}...release": compared(1, ["web/app.py"]),
        }
    )
    freshness.refresh(store, gh, cfg)
    rows = store.clip_freshness()
    assert (rows["bare"]["state"], rows["bare"]["branch"]) == ("check", "main")  # a .py file isn't UI by default
    assert (rows["full"]["state"], rows["full"]["branch"]) == ("redo", "release")
    store.close()


def test_clips_config_is_checked(tmp_path):
    cfg = config(tmp_path)
    assert cfg.clips.for_demo("atlas") == ("dev", DEFAULT_UI_PATHS, DEFAULT_IGNORE_PATHS)
    assert DEFAULT_UI_PATHS == (
        "*.css",
        "*.scss",
        "*.html",
        "*.tsx",
        "*.jsx",
        "*.ts",
        "*.js",
        "*.mjs",
        "*.svelte",
        "*.astro",
        "*.vue",
    )
    for bad in (
        "clips: [x]\n",
        "clips: {owner: 'not an owner!'}\n",
        "clips: {branch: '../x'}\n",
        "clips: {ui_paths: []}\n",
        "clips: {ui_paths: src/**}\n",
        "clips: {ignore_paths: tests/**}\n",
        "clips: {ignore_paths: [1]}\n",
        "clips: {ignore_paths: {a: b}}\n",
        "clips: {demos: {atlas: {ui_paths: []}}}\n",
        "clips: {demos: {atlas: {ignore_paths: docs/**}}}\n",
        "clips: {demos: {atlas: {colour: red}}}\n",
        "clips: {colour: red}\n",
    ):
        with pytest.raises(ConfigError):
            config(tmp_path, bad)


def test_ignore_paths_are_set_globally_and_per_demo(tmp_path):
    # Your own ui_paths are kept exactly, and the default ignore_paths still apply to them.
    cfg = config(tmp_path, "clips:\n  ui_paths: [web/**]\n")
    assert cfg.clips.for_demo("atlas") == ("dev", ("web/**",), DEFAULT_IGNORE_PATHS)

    # A global list replaces the default; an empty one turns ignoring off.
    cfg = config(tmp_path, "clips:\n  ignore_paths: [web/vendor/**]\n")
    assert cfg.clips.for_demo("atlas")[2] == ("web/vendor/**",)
    assert config(tmp_path, "clips: {ignore_paths: []}\n").clips.for_demo("atlas")[2] == ()

    # A demo's own list replaces the global one; a demo without one inherits it.
    cfg = config(
        tmp_path,
        "clips:\n  ignore_paths: [gen/**]\n  demos:\n"
        "    lens: {ignore_paths: [web/vendor/**]}\n    realms: {ignore_paths: []}\n    atlas: {branch: main}\n",
    )
    assert cfg.clips.for_demo("lens")[2] == ("web/vendor/**",)
    assert cfg.clips.for_demo("realms")[2] == ()
    assert cfg.clips.for_demo("atlas") == ("main", DEFAULT_UI_PATHS, ("gen/**",))
    assert cfg.clips.for_demo("other")[2] == ("gen/**",)

    # And refresh uses them: a change to a test file is a check under the default ignore_paths,
    # and a redo for the demo that turned ignoring off.
    cfg = config(tmp_path, "clips:\n  demos:\n    lens: {ignore_paths: []}\n")
    store = Store(cfg.store_path)
    recorded(store, "atlas")
    recorded(store, "lens", demo="lens")
    gh = GitHub({f"smk762/argus-realms/compare/{BASE}...dev": compared(1, ["tests/ui/x.js"])})
    freshness.refresh(store, gh, cfg)
    rows = store.clip_freshness()
    assert (rows["atlas"]["state"], rows["lens"]["state"]) == ("check", "redo")
    assert json.loads(rows["lens"]["ui_files"]) == ["tests/ui/x.js"]
    store.close()


def test_failed_call_keeps_the_last_state_and_says_how_old(tmp_path):
    cfg = config(tmp_path, "clips: {owner: smk762}\n")
    store = Store(cfg.store_path)
    recorded(store, "ui", demo_repo="argus-realms")
    url = f"smk762/argus-realms/compare/{BASE}...dev"
    gh = GitHub({url: compared(2, ["src/a.ts"])})
    freshness.refresh(store, gh, cfg)
    store.conn.execute("UPDATE clip_freshness SET checked_at = ?", ("2026-11-01T00:00:00+00:00",))

    gh.answers[url] = 403
    summary = freshness.refresh(store, gh, cfg)
    assert summary.errors == ["ui: GET api.github.com failed with HTTP 403"]
    row = store.clip_freshness()["ui"]
    assert (row["state"], row["commits"], row["checked_at"]) == ("redo", 2, "2026-11-01T00:00:00+00:00")
    now = datetime(2026, 11, 4, 1, tzinfo=UTC)
    clip = store.clip("ui")
    assert freshness.label(clip, row, now) == "redo (as of 3d)"
    assert freshness.explain(clip, row, now)[-2:] == [
        "Checked 3d ago.",
        f"The last check failed {freshness.ago(row['failed_at'], now)} ago: GET api.github.com failed with HTTP 403",
    ]

    # A success clears the failure. A failure with no earlier result is still recorded.
    gh.answers[url] = compared(0)
    freshness.refresh(store, gh, cfg)
    assert store.clip_freshness()["ui"]["failed_at"] == "" and store.clip_freshness()["ui"]["state"] == "fresh"
    recorded(store, "new", demo_repo="argus-realms", demo_commit="eeeeeee")
    freshness.refresh(store, gh, cfg)
    row = store.clip_freshness()["new"]
    assert (row["state"], row["checked_at"]) == ("unknown", "") and "HTTP 404" in row["error"]
    assert freshness.label(store.clip("new"), row) == "unknown"
    store.close()


def test_ack_moves_the_clip_forward(tmp_path):
    cfg = config(tmp_path, "clips: {owner: smk762}\n")
    store = Store(cfg.store_path)
    store.baseline("argus-atlas", ["0.4.0", "0.5.1"])
    recorded(store, "ui")
    with pytest.raises(ReviewError, match="hasn't been compared"):
        clips.ack(store, "ui")
    gh = GitHub({f"smk762/argus-realms/compare/{BASE}...dev": compared(4, ["src/a.ts"])})
    freshness.refresh(store, gh, cfg)
    assert freshness.state(store.clip("ui"), store.clip_freshness()["ui"]) == "redo"

    # Changed by hand since the check: the check no longer describes it, so there is nothing to ack.
    clips.update(store, "ui", demo_commit="fffffff")
    assert freshness.state(store.clip("ui"), store.clip_freshness()["ui"]) == ""
    with pytest.raises(ReviewError, match="hasn't been compared"):
        clips.ack(store, "ui")
    clips.update(store, "ui", demo_commit=BASE)

    clip = clips.ack(store, "ui")
    assert (clip["demo_commit"], clip["project_version"], clip["status"]) == (HEAD, "0.5.1", "recorded")
    row = store.clip_freshness()["ui"]
    assert freshness.state(clip, row) == "fresh" and freshness.explain(clip, row)[-1].startswith("Acknowledged")

    # A stale clip isn't checked, and herald never changes a status itself.
    clips.update(store, "ui", status="stale")
    gh.calls.clear()
    freshness.refresh(store, gh, cfg)
    assert gh.calls == [] and store.clip("ui")["status"] == "stale"
    assert freshness.label(store.clip("ui"), store.clip_freshness()["ui"]) == "-"
    store.close()


def test_collect_refreshes_and_survives_a_failure(tmp_path):
    from test_metrics import Fake, _setup

    from argus_herald.metrics import Metrics, collect

    class WithCompare(Fake):
        def __init__(self, status):
            super().__init__()
            self.status = status

        def request(self, method, url, body=None, headers=None):
            if "/compare/" in url:
                self.calls.append((url, dict(headers or {})))
                return (200, json.dumps(compared(1, ["src/a.ts"])).encode()) if self.status == 200 else (500, b"")
            return super().request(method, url, body, headers)

    cfg, store, metrics = _setup(tmp_path)
    recorded(store, "ui", demo_repo="argus-realms")  # owner from pypi.discover.github_owner
    report = collect(cfg, store, metrics, WithCompare(200), {})
    assert "clip_freshness: 1 clips: 1 redo" in report.lines
    assert any(line.startswith("pypi_downloads") for line in report.lines)

    other = Metrics(tmp_path / "m2.sqlite")
    report = collect(cfg, store, other, WithCompare(500), {})
    assert report.errors == ["clip_freshness: ui: GET api.github.com failed with HTTP 500"]
    assert any(line.startswith("pypi_downloads") for line in report.lines)  # the rest still ran
    assert store.clip_freshness()["ui"]["state"] == "redo"
    metrics.close()
    other.close()
    store.close()


def test_cli_lists_redo_first_with_reasons(tmp_path, capsys, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("pypi:\n  projects: [argus-atlas]\nclips:\n  owner: smk762\n", encoding="utf-8")
    base = ["--config", str(cfg), "--env", str(tmp_path / "no.env"), "--store", str(tmp_path / "s.sqlite")]
    store = Store(tmp_path / "s.sqlite")
    recorded(store, "a-fresh", demo_commit="aaaaaaa")
    recorded(store, "b-todo", status="todo")
    recorded(store, "c-redo", demo_commit="ccccccc", stale_if="the legend moves")
    recorded(store, "d-unknown", demo_repo="")
    recorded(store, "e-check", demo_commit="eeeeeee")
    store.close()
    gh = GitHub(
        {
            "smk762/argus-realms/compare/aaaaaaa...dev": compared(0),
            "smk762/argus-realms/compare/ccccccc...dev": compared(5, ["src/legend.ts"]),
            "smk762/argus-realms/compare/eeeeeee...dev": compared(1, ["CHANGELOG.md"]),
        }
    )
    monkeypatch.setattr("argus_herald.cli.HttpClient", lambda: gh)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    assert main([*base, "clips", "ack", "c-redo"]) == 2  # nothing compared yet
    assert main([*base, "clips", "refresh"]) == 0
    assert "Checked 4 clips: 1 redo, 1 check, 1 unknown, 1 fresh" in capsys.readouterr().out
    assert main([*base, "clips"]) == 0
    out = capsys.readouterr().out
    order = [line.split()[0] for line in out.splitlines()[1:6]]
    assert order == ["c-redo", "e-check", "d-unknown", "a-fresh", "b-todo"]
    assert out.splitlines()[0].split()[:5] == ["clip", "demo", "format", "status", "fresh"]
    assert "  c-redo (redo): 5 commits on dev since ccccccc; 1 UI file changed: src/legend.ts." in out
    assert "    Stale if: the legend moves" in out
    assert "  e-check (check): 1 commit on dev since eeeeeee; 1 file changed, none under the UI paths." in out
    assert "a-fresh (fresh)" not in out

    assert main([*base, "clips", "show", "c-redo"]) == 0
    assert "Freshness: redo\n  5 commits on dev" in capsys.readouterr().out
    assert main([*base, "clips", "ack", "c-redo"]) == 0
    assert f"demo_commit={HEAD}" in capsys.readouterr().out
    assert main([*base, "clips", "show", "b-todo"]) == 0
    assert "Freshness: not checked while todo" in capsys.readouterr().out
    assert main([*base, "clips", "refresh", "x"]) == 2

    gh.answers.clear()  # every call now fails: the last state stays, and the command says so
    assert main([*base, "clips", "refresh"]) == 1
    assert "error e-check: GET api.github.com failed with HTTP 404" in capsys.readouterr().err
    assert main([*base, "clips"]) == 0
    assert re.search(r"(?m)^e-check +atlas +clip +recorded +check \(as of \d+s\)", capsys.readouterr().out)


def test_removing_a_clip_drops_its_check(tmp_path):
    cfg = config(tmp_path, "clips: {owner: smk762}\n")
    store = Store(cfg.store_path)
    recorded(store, "ui")
    freshness.refresh(store, GitHub({f"smk762/argus-realms/compare/{BASE}...dev": compared(0)}), cfg)
    clips.remove(store, "ui")
    assert store.clip_freshness() == {}
    assert freshness.ago((datetime.now(UTC) - timedelta(minutes=5)).isoformat()) == "5m"
    store.close()


def test_a_timestamp_without_an_offset_is_read_as_utc():
    now = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    assert freshness.ago("2026-11-21T10:00:00", now) == "2h"
    assert freshness.ago("2026-11-21T10:00:00+00:00", now.replace(tzinfo=None)) == "2h"
