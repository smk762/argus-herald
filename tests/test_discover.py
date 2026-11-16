import json

from argus_herald.config import load_config
from argus_herald.discover import check_projects


class Http:
    """PyPI knows `known`; GitHub lists `repos` whose pyproject names are in `pyprojects`."""

    def __init__(self, known, repos, pyprojects, profile=None):
        self.known, self.repos, self.pyprojects, self.profile = set(known), repos, pyprojects, profile
        self.calls = []

    def request(self, method, url, body=None, headers=None):
        self.calls.append((url, dict(headers or {})))
        if url.startswith("https://pypi.org/pypi/"):
            name = url.split("/")[4]
            return (200, b"{}") if name in self.known else (404, b"{}")
        if url.startswith("https://pypi.org/user/"):
            return (200, self.profile.encode()) if self.profile else (403, b"Client Challenge")
        if url.startswith("https://api.github.com/users/"):
            return 200, json.dumps(self.repos).encode()
        if url.startswith("https://raw.githubusercontent.com/"):
            repo = url.split("/")[4]
            name = self.pyprojects.get(repo)
            return (200, f'[project]\nname = "{name}"\n'.encode()) if name else (404, b"")
        return 404, b""


def _config(tmp_path, projects, extra=""):
    path = tmp_path / "c.yaml"
    path.write_text(
        "pypi:\n  user: someone\n  projects: [" + ", ".join(projects) + "]\n"
        "  discover: {github_owner: smk762, prefix: argus-}\n" + extra + "destinations:\n  patreon: {enabled: true}\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    cfg.store_path = tmp_path / "s.sqlite"
    return cfg


REPOS = [
    {"name": "argus-atlas"},
    {"name": "argus-mask"},
    {"name": "argus-realms"},
    {"name": "argus-studio"},
    {"name": "argus-fork", "fork": True},
    {"name": "argus-old", "archived": True},
    {"name": "other"},
]
PYPROJECTS = {
    "argus-atlas": "argus-atlas",
    "argus-mask": "argus-mask",
    "argus-realms": "atlas-realms",
    "argus-fork": "argus-fork",
    "argus-old": "argus-old",
    "other": "other-pkg",
}
KNOWN = {"argus-atlas", "argus-mask", "argus-fork", "argus-old", "other-pkg"}


def test_finds_unconfigured_github_packages_and_missing_names(tmp_path):
    cfg = _config(tmp_path, ["argus-atlas", "argus-typo"])
    http = Http(KNOWN, REPOS, PYPROJECTS)
    drift = check_projects(cfg, http, {"GITHUB_TOKEN": "t"})
    assert drift.missing == ["argus-typo"]
    # realms isn't on PyPI; forks, archived and non-prefixed repos are skipped
    assert drift.unconfigured == {"argus-mask": "github.com/smk762/argus-mask"}
    assert any(note.startswith("PyPI profile someone not checked") for note in drift.notes)
    github = [h for u, h in http.calls if "api.github.com" in u]
    assert github[0]["Authorization"] == "Bearer t"


def test_profile_and_ignore_list_are_used(tmp_path):
    cfg = _config(tmp_path, ["argus-atlas", "argus-mask"], "  ignore: [argus-legacy]\n")
    profile = "".join(
        f'<a class="package-snippet__name">{name}</a>' for name in ("argus-atlas", "argus-extra", "argus-legacy")
    )
    drift = check_projects(cfg, Http(KNOWN | {"argus-extra", "argus-legacy"}, REPOS, PYPROJECTS, profile), {})
    assert drift.unconfigured == {"argus-extra": "PyPI profile someone"} and not drift.missing


def test_cli_exits_1_on_drift_and_0_when_clean(tmp_path, capsys):
    from argus_herald.cli import cmd_discover_check

    cfg = _config(tmp_path, ["argus-atlas"])
    assert cmd_discover_check(cfg, Http(KNOWN, REPOS, PYPROJECTS), {}) == 1
    assert "  - argus-mask    # github.com/smk762/argus-mask" in capsys.readouterr().out

    cfg = _config(tmp_path, ["argus-atlas", "argus-mask"])
    assert cmd_discover_check(cfg, Http(KNOWN, REPOS, PYPROJECTS), {}) == 0
    assert "no drift" in capsys.readouterr().out
