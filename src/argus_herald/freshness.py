"""Clip freshness: does a recorded clip still match the live demo?

A clip was recorded against one demo commit and one package version. The check compares those
with the head of the demo repo's deploy branch (GitHub's compare API) and with the newest
version herald has seen for the package:

- fresh: no commits since, and the same minor version;
- check: the repo changed, but no UI file; or the recorded commit isn't on the deploy branch; or the
  minor version moved;
- redo: UI files changed.

A UI file matches one of the demo's UI paths and none of its ignore paths. By default the UI
paths are front-end file types (`*.css`, `*.scss`, `*.html`, `*.tsx`, `*.jsx`, `*.ts`, `*.js`,
`*.mjs`, `*.svelte`, `*.astro`, `*.vue`), so a backend-only commit (say `src/pkg/api.py`) asks
for a check, not a redo. The ignore paths leave out tests, docs, scripts, tool config and type
declarations (`**/tests/**`, `**/test/**`, `**/__tests__/**`, `**/docs/**`, `**/scripts/**`,
`**/e2e/**`, `*.test.*`, `*.spec.*`, `*.config.*`, `*.d.ts`). Both are set under `clips:`, and a
demo's own list replaces the global one.

Herald never changes a clip's status. A person looks, then acks the clip (it still matches) or
marks it stale. The last result is kept in the draft store, so the listing works offline and a
failed call leaves the last known state.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache

from argus_herald.config import Config
from argus_herald.github import COMPARE_FILES, Comparison, compare
from argus_herald.http import Http, HttpError
from argus_herald.models import CLIP_CURRENT
from argus_herald.store import Store
from argus_herald.workflow import ReviewError

FRESH, CHECK, REDO, UNKNOWN = "fresh", "check", "redo", "unknown"
ORDER = (REDO, CHECK, UNKNOWN, FRESH)  # most urgent first; clips not checked come last
FLAGGED = (REDO, CHECK, UNKNOWN)
SHOW_FILES = 3
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


# ---- what counts as UI -------------------------------------------------------------------


@cache
def _glob(pattern: str) -> re.Pattern[str]:
    """`**` crosses directories, `*` and `?` don't. A pattern without `/` is matched against the
    file name at any depth, so `*.css` means every stylesheet."""
    pattern = pattern.strip().lstrip("/")
    if "/" not in pattern:
        pattern = "**/" + pattern
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def matches(path: str, pattern: str) -> bool:
    return _glob(pattern).fullmatch(path) is not None


def ui_files(files: tuple[str, ...] | list[str], globs: tuple[str, ...], ignore: tuple[str, ...] = ()) -> list[str]:
    """The files matching a UI glob and no ignore glob."""
    return [
        path for path in files if any(matches(path, g) for g in globs) and not any(matches(path, g) for g in ignore)
    ]


# ---- versions ----------------------------------------------------------------------------


def minor(version: str) -> tuple[int, int] | None:
    from argus_herald.digest import parse_version

    parsed = parse_version(version or "")
    return parsed[:2] if parsed else None


def newest_version(versions: list[str]) -> str:
    """The highest stable version; a prerelease only when there is nothing else."""
    from argus_herald.digest import is_prerelease, parse_version

    ranked = [(not is_prerelease(v), parse_version(v), v) for v in versions if parse_version(v)]
    return max(ranked)[2] if ranked else ""


def minor_moved(recorded: str, newest: str) -> bool:
    before, now = minor(recorded), minor(newest)
    return before is not None and now is not None and now > before


# ---- classifying one clip ------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    state: str
    ui_files: tuple[str, ...] = ()


def classify(
    comparison: Comparison | None, recorded: str, newest: str, globs: tuple[str, ...], ignore: tuple[str, ...] = ()
) -> Verdict:
    """One clip's state. `comparison` is None when the demo source couldn't be compared: a moved
    minor version still says `check`, otherwise the state is unknown."""
    moved = minor_moved(recorded, newest)
    if comparison is None:
        return Verdict(CHECK if moved else UNKNOWN)
    changed = tuple(ui_files(comparison.files, globs, ignore))
    if changed:
        return Verdict(REDO, changed)
    # behind_by: the recorded commit isn't on the deploy branch, so the live demo lacks what the clip shows.
    if comparison.ahead_by or comparison.behind_by or moved:
        return Verdict(CHECK)
    return Verdict(FRESH)


def locate(clip: dict, config: Config) -> tuple[str, str] | str:
    """(owner, repo) of a clip's demo source, or why it can't be compared."""
    if not clip["demo_repo"]:
        return "no demo_repo recorded"
    if not clip["demo_commit"]:
        return "no demo_commit recorded"
    name = re.sub(r"^https://github\.com/", "", clip["demo_repo"].strip()).removesuffix(".git").strip("/")
    owner, _, repo = name.rpartition("/")
    if not owner:
        github = config.github.owner if config.github else ""
        owner = config.clips.owner or github or config.discover_owner or ""
        if not owner:
            return (
                f"demo_repo {name} has no owner; write it as owner/{name} or set clips.owner, "
                "github.owner or pypi.discover.github_owner in the config"
            )
    if not _NAME.match(owner) or not _NAME.match(repo) or "/" in owner or {owner, repo} & {".", ".."}:
        return f"demo_repo {clip['demo_repo']!r} isn't a GitHub repo (owner/repo)"
    return owner, repo


# ---- refreshing every clip ---------------------------------------------------------------


@dataclass
class Summary:
    states: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)

    @property
    def line(self) -> str:
        checked = sum(self.states.values()) + len(self.errors)
        parts = [f"{self.states[s]} {s}" for s in ORDER if self.states[s]]
        failed = [f"{len(self.errors)} failed"] if self.errors else []
        return f"{checked} clips: {', '.join(parts + failed) or 'none to check'}"


def refresh(store: Store, http: Http, config: Config, token: str = "") -> Summary:
    """Compare every recorded, edited or posted clip with its demo and package now."""
    summary = Summary()
    results: dict[tuple[str, str, str, str], Comparison | Exception] = {}
    for clip in store.clips():
        if clip["status"] not in CLIP_CURRENT:
            continue
        branch, globs, ignore = config.clips.for_demo(clip["demo"])
        newest = newest_version(store.seen_versions_named(clip["project"])) if clip["project"] else ""
        base = {"base_commit": clip["demo_commit"], "base_version": clip["project_version"]}
        where = locate(clip, config)
        comparison = None
        if isinstance(where, tuple):
            key = (*where, clip["demo_commit"].lower(), branch)
            if key not in results:  # clips of one demo commit share a call
                try:
                    results[key] = compare(http, *where, clip["demo_commit"].lower(), branch, token)
                except (HttpError, OSError, ValueError) as exc:
                    results[key] = exc
            if isinstance(results[key], Exception):
                store.freshness_failed(clip["id"], str(results[key]), **base)
                summary.errors.append(f"{clip['id']}: {results[key]}")
                continue
            comparison = results[key]
        verdict = classify(comparison, clip["project_version"], newest, globs, ignore)
        if comparison is None:
            reason = f"demo source not compared: {where}"
        elif comparison.behind_by:
            reason = (
                f"recorded commit {clip['demo_commit'][:7]} isn't on {branch}: "
                f"{comparison.behind_by} commit{'s' * (comparison.behind_by != 1)} it has are missing there"
            )
        else:
            reason = ""
        store.save_freshness(
            clip["id"],
            state=verdict.state,
            commits=comparison.ahead_by if comparison else 0,
            ui_files=json.dumps(list(verdict.ui_files)),
            files_changed=len(comparison.files) if comparison else 0,
            truncated=int(comparison.truncated) if comparison else 0,
            head_commit=comparison.head_commit if comparison else "",
            branch=branch,
            newest_version=newest,
            reason=reason,
            **base,
        )
        summary.states[verdict.state] += 1
    return summary


def ack(store: Store, clip_id: str) -> dict:
    """The person looked and the clip still matches: move what it was recorded against up to
    what the last check found."""
    from argus_herald import clips

    clip = clips.require(store, clip_id)
    row = current(clip, store.clip_freshness().get(clip["id"]))
    if row is None or not row["checked_at"]:
        raise ReviewError(f"Clip {clip['id']} hasn't been compared with its demo yet; run `argus-herald clips refresh`")
    changes = {}
    if row["head_commit"]:
        changes["demo_commit"] = row["head_commit"]
    newest = row["newest_version"]
    if newest and (minor(clip["project_version"]) is None or _later(newest, clip["project_version"])):
        changes["project_version"] = newest
    if changes:
        clip = clips.update(store, clip["id"], **changes)
    store.save_freshness(
        clip["id"],
        state=FRESH,
        head_commit=row["head_commit"],
        branch=row["branch"],
        newest_version=newest,
        base_commit=clip["demo_commit"],
        base_version=clip["project_version"],
        reason="acknowledged",
    )
    return clip


def _later(version: str, than: str) -> bool:
    from argus_herald.digest import parse_version

    a, b = parse_version(version), parse_version(than)
    return a is not None and b is not None and a > b


# ---- reading the stored result ---------------------------------------------------------------


def current(clip: dict, row: dict | None) -> dict | None:
    """The stored check, if it still describes the clip: one that isn't checked (stale, todo...)
    has none, and neither has one whose commit or version was changed by hand since."""
    if row is None or clip["status"] not in CLIP_CURRENT:
        return None
    if (row["base_commit"], row["base_version"]) != (clip["demo_commit"], clip["project_version"]):
        return None
    return row


def state(clip: dict, row: dict | None) -> str:
    """fresh / check / redo / unknown, or "" when the clip isn't checked."""
    row = current(clip, row)
    return row["state"] if row else ""


def rank(clip: dict, row: dict | None) -> int:
    found = state(clip, row)
    return ORDER.index(found) if found in ORDER else len(ORDER)


def label(clip: dict, row: dict | None, now: datetime | None = None) -> str:
    """The listing's column: the state, with its age when the last check failed."""
    row = current(clip, row)
    if row is None:
        return "-"
    if row["failed_at"] and row["checked_at"]:
        return f"{row['state']} (as of {ago(row['checked_at'], now)})"
    return row["state"]


def explain(clip: dict, row: dict | None, now: datetime | None = None) -> list[str]:
    """Why a clip has its state, one sentence a line. Empty for a clip that isn't checked."""
    row = current(clip, row)
    if row is None:
        return []
    lines = []
    if row["reason"] and row["reason"] != "acknowledged":
        lines.append(row["reason"][:1].upper() + row["reason"][1:] + ".")
    if row["commits"]:
        commits = (
            f"{row['commits']} commit{'s' * (row['commits'] != 1)} on {row['branch']} since {row['base_commit'][:7]}"
        )
        changed = json.loads(row["ui_files"] or "[]")
        if changed:
            shown = ", ".join(changed[:SHOW_FILES]) + (
                f" +{len(changed) - SHOW_FILES} more" if len(changed) > SHOW_FILES else ""
            )
            commits += f"; {len(changed)} UI file{'s' * (len(changed) != 1)} changed: {shown}"
        elif row["files_changed"]:
            commits += (
                f"; {row['files_changed']} file{'s' * (row['files_changed'] != 1)} changed, none under the UI paths"
            )
        if row["truncated"]:
            commits += f" (GitHub lists only the first {COMPARE_FILES} changed files, so there may be more)"
        lines.append(commits + ".")
    newest, recorded = row["newest_version"], row["base_version"]
    if newest and recorded and _later(newest, recorded):
        moved = "minor version moved" if minor_moved(recorded, newest) else "patch only"
        lines.append(f"{clip['project']} {recorded} -> {newest} ({moved}).")
    if row["state"] in FLAGGED and clip["stale_if"]:
        lines.append(f"Stale if: {clip['stale_if']}")
    if row["checked_at"]:
        when = "Acknowledged" if row["reason"] == "acknowledged" else "Checked"
        lines.append(f"{when} {ago(row['checked_at'], now)} ago.")
    if row["failed_at"]:
        lines.append(f"The last check failed {ago(row['failed_at'], now)} ago: {row['error']}")
    return lines


def ago(iso: str, now: datetime | None = None) -> str:
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return "a while"
    now = now or datetime.now(UTC)
    # A stamp without an offset is taken as UTC, so naive and aware values can be subtracted.
    then = then if then.tzinfo else then.replace(tzinfo=UTC)
    now = now if now.tzinfo else now.replace(tzinfo=UTC)
    seconds = max(0, int((now - then).total_seconds()))
    for size, unit in ((86_400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
