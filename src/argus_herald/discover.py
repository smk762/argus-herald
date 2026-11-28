"""Find PyPI projects the config should track but doesn't.

PyPI's JSON API answers for a name you already know; it can't list projects by prefix. So
discovery has three parts:

1. every configured name must exist on PyPI (catches typos and removed projects);
2. when the PyPI profile page is readable (it often answers with a client challenge), it is
   compared with the configured list;
3. the GitHub owner's public, non-fork repos whose names start with the prefix are read for a
   ``pyproject.toml``; a project name declared there that exists on PyPI must be configured.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import quote

from packaging.utils import canonicalize_name

from argus_herald.config import Config
from argus_herald.http import Http, HttpError
from argus_herald.pypi import ProfileUnavailable, discover_projects, project_json_url
from argus_herald.workflow import tracked_projects

GITHUB_API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
MAX_PAGES = 10


@dataclass
class Drift:
    missing: list[str] = field(default_factory=list)  # configured but not on PyPI
    unconfigured: dict[str, str] = field(default_factory=dict)  # name -> where it was found
    notes: list[str] = field(default_factory=list)  # checks that couldn't run

    @property
    def clean(self) -> bool:
        return not self.missing and not self.unconfigured


def check_projects(config: Config, http: Http, env: Mapping[str, str]) -> Drift:
    drift = Drift()
    tracked = tracked_projects(config)
    configured = {canonicalize_name(name) for name in config.projects}
    ignored = {canonicalize_name(name) for name in config.ignore}

    for name in tracked:
        try:
            status = _pypi_status(http, name)
        except OSError as exc:  # HttpError is answered by _pypi_status; this is DNS, timeouts, resets
            drift.notes.append(f"PyPI not checked for {name}: {exc}")
            continue
        if status == 404:
            drift.missing.append(name)
        elif status != 200:
            drift.notes.append(f"PyPI answered HTTP {status} for {name}")

    def found(name: str, where: str) -> None:
        key = canonicalize_name(name)
        if (
            key not in configured
            and key not in ignored
            and key not in {canonicalize_name(n) for n in drift.unconfigured}
        ):
            drift.unconfigured[name] = where

    for user in config.users:
        try:
            for name in discover_projects(http, user):
                found(name, f"PyPI profile {user}")
        except (ProfileUnavailable, HttpError, OSError) as exc:
            drift.notes.append(f"PyPI profile {user} not checked: {exc}")

    owner, prefix = config.discover_owner, config.discover_prefix
    if owner:
        token = (env.get("GITHUB_TOKEN") or "").strip()
        try:
            repos = _github_repos(http, owner, token)
        except (HttpError, OSError, ValueError) as exc:
            drift.notes.append(f"GitHub repos of {owner} not checked: {exc}")
            repos = []
        for repo in repos:
            if not repo.startswith(prefix):
                continue
            try:
                name = _pyproject_name(http, owner, repo)
                if name and canonicalize_name(name) not in configured and _pypi_status(http, name) == 200:
                    found(name, f"github.com/{owner}/{repo}")
            except (HttpError, OSError, ValueError) as exc:
                drift.notes.append(f"github.com/{owner}/{repo} not checked: {exc}")
    return drift


def _pypi_status(http: Http, name: str) -> int:
    try:
        status, _ = http.request("GET", project_json_url(name), headers={"Accept": "application/json"})
    except HttpError as exc:
        return exc.status
    return status


def _github_repos(http: Http, owner: str, token: str) -> list[str]:
    """Public, non-fork, non-archived repos of a user or organisation."""
    return sorted(str(repo["name"]) for repo in github_repo_info(http, owner, token))


def github_repo_info(http: Http, owner: str, token: str) -> list[dict]:
    """The GitHub API records of an owner's public, non-fork, non-archived repos."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    repos: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{GITHUB_API}/users/{quote(owner, safe='')}/repos?type=owner&per_page=100&page={page}"
        status, body = http.request("GET", url, headers=headers)
        if status != 200:
            raise HttpError("GET", url, status)
        batch = json.loads(body.decode("utf-8"))
        if not isinstance(batch, list):
            raise ValueError("Unexpected GitHub response")
        for repo in batch:
            if (
                isinstance(repo, dict)
                and repo.get("name")
                and not repo.get("fork")
                and not repo.get("archived")
                and not repo.get("private")
            ):
                repos.append(repo)
        if len(batch) < 100:
            break
    return repos


def _pyproject_name(http: Http, owner: str, repo: str) -> str | None:
    url = f"{RAW}/{quote(owner, safe='')}/{quote(repo, safe='')}/HEAD/pyproject.toml"
    try:
        status, body = http.request("GET", url, headers={"Accept": "text/plain"})
    except HttpError:
        return None
    if status != 200 or len(body) > 200_000:
        return None
    try:
        data = tomllib.loads(body.decode("utf-8", "replace"))
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    name = project.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None
