"""The clip registry: what each demo clip shows and what it was recorded against.

A clip is one row in the draft store, keyed by a short id the person chooses. Attaching a clip to
a draft (or naming it on a hand-logged broadcast) records the link, so later questions (which
demos have no clip, which clips are out of date, which did well) have something to join on.

The registry moves in and out as CSV with its own column names. A list kept by hand before the
registry existed used a few other names; `import_csv` reads those too.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from argus_herald import freshness
from argus_herald.models import CLIP_CURRENT, CLIP_FIELDS, CLIP_FORMATS, CLIP_STATUSES
from argus_herald.performance import NO_DATA, RATINGS, STRONG
from argus_herald.store import Store
from argus_herald.workflow import ReviewError

if TYPE_CHECKING:
    from argus_herald.performance import ClipPerformance

COLUMNS = ("id", *CLIP_FIELDS)
# Column names from a hand-kept list, and the registry column each one fills.
ALIASES = {
    "clip_id": "id",
    "package": "project",
    "target_len_s": "target_seconds",
    "recorded_on": "recorded_at",
    "pkg_version_recorded": "project_version",
    "demo_commit_recorded": "demo_commit",
    "file": "path",
}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_COMMIT = re.compile(r"^[0-9a-fA-F]{7,40}$")
_MAX_TEXT = 500


def clean_fields(fields: dict[str, object]) -> dict[str, object]:
    """Validate and normalise clip fields (any subset of CLIP_FIELDS). Raises ReviewError."""
    out: dict[str, object] = {}
    for key, raw in fields.items():
        if key not in CLIP_FIELDS:
            raise ReviewError(f"Unknown clip field {key}")
        value = "" if raw is None else str(raw).strip()
        if key == "format":
            value = value.lower() or "clip"
            if value not in CLIP_FORMATS:
                raise ReviewError(f"format is one of: {', '.join(CLIP_FORMATS)} (not {value!r})")
        elif key == "status":
            value = value.lower() or "todo"
            if value not in CLIP_STATUSES:
                raise ReviewError(f"status is one of: {', '.join(CLIP_STATUSES)} (not {value!r})")
        elif key == "target_seconds":
            if not value:
                out[key] = None
                continue
            if not value.isdigit() or not 0 < int(value) <= 86_400:
                raise ReviewError(f"target_seconds is a whole number of seconds, not {value!r}")
            out[key] = int(value)
            continue
        elif key == "recorded_at" and value:
            try:
                date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ReviewError(f"recorded_at is an ISO date or timestamp, not {value!r}") from exc
        elif key == "demo_commit" and value and not _COMMIT.match(value):
            raise ReviewError(f"demo_commit is a commit hash (7 to 40 hex digits), not {value!r}")
        elif key == "path" and value:
            # Absolute: the file is attached later, from whatever directory that command runs in.
            value = str(Path(value).expanduser().absolute())
        if len(value) > _MAX_TEXT:
            raise ReviewError(f"{key} is longer than {_MAX_TEXT} characters")
        out[key] = value
    return out


def clean_id(clip_id: str) -> str:
    clip_id = (clip_id or "").strip()
    if not _ID.match(clip_id):
        raise ReviewError("A clip id is a short slug: letters, digits, '.', '-' and '_' (like atlas-styles)")
    return clip_id


def require(store: Store, clip_id: str) -> dict:
    clip = store.clip(clip_id.strip())
    if clip is None:
        raise ReviewError(f"No clip {clip_id!r}. `argus-herald clips` lists them.")
    return clip


def add(store: Store, clip_id: str, **fields: object) -> dict:
    clip_id = clean_id(clip_id)
    if store.clip(clip_id) is not None:
        raise ReviewError(f"Clip {clip_id} already exists; change it with `clips set {clip_id} ...`")
    store.save_clips([{"id": clip_id, **_stamp(None, clean_fields(fields))}])
    return require(store, clip_id)


def update(store: Store, clip_id: str, **changes: object) -> dict:
    clip = require(store, clip_id)
    if not changes:
        raise ReviewError("Nothing to change. Name a field, like --status recorded.")
    store.save_clips([{"id": clip["id"], **_stamp(clip, clean_fields(changes))}])
    return require(store, clip["id"])


def _stamp(before: dict | None, fields: dict[str, object]) -> dict[str, object]:
    """A clip that becomes `recorded` (first take or a re-record) was recorded today, unless a date is given."""
    was = before["status"] if before else None
    if fields.get("status") == "recorded" and was != "recorded" and "recorded_at" not in fields:
        return {**fields, "recorded_at": datetime.now(UTC).date().isoformat()}
    return fields


def attach(
    store: Store, media_root: Path, draft_id: int, clip_id: str, alt: str, *, all_drafts: bool = False
) -> list[int]:
    """Attach a clip's file to a draft like any other media (cleaned copy, alt text, the gate on
    approve), and record which clip it was. Returns media ids."""
    from argus_herald.workflow import attach_media

    clip = require(store, clip_id)
    if not clip["path"]:
        raise ReviewError(f"Clip {clip['id']} has no file yet; set one with `clips set {clip['id']} --path <file>`")
    source = Path(clip["path"])
    return attach_media(
        store,
        media_root,
        draft_id,
        source,
        alt,
        all_drafts=all_drafts,
        # The clip id, not the file's own name: the name is shown in review and may be posted.
        name=f"{clip['id']}{source.suffix.lower()}",
        clip_id=clip["id"],
    )


def link_broadcast(store: Store, broadcast_id: int, clip_id: str) -> None:
    """Record that a hand-logged broadcast used a clip."""
    clip = require(store, clip_id)
    if not store.set_logged_clip(broadcast_id, clip["id"]):
        raise ReviewError(f"No logged broadcast {broadcast_id}")


def remove(store: Store, clip_id: str) -> None:
    clip = require(store, clip_id)
    uses = store.clip_uses(clip["id"])
    if uses:
        raise ReviewError(f"Clip {clip['id']} is used by {len(uses)} post(s); set its status to hold instead")
    store.delete_clip(clip["id"])


def import_csv(store: Store, text: str) -> tuple[int, int, list[str]]:
    """Add or update clips from CSV text. Every row is checked before any is saved.

    A column in the file replaces the stored value, empty cells included; a column not in the
    file is left alone. Returns (added, updated, column names that were ignored)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = [(name or "").strip() for name in reader.fieldnames or []]
    mapped = {name: ALIASES.get(name, name) for name in header}
    if "id" not in mapped.values():
        raise ReviewError("The CSV needs an id column (or clip_id)")
    # From the header, not the mapping: the same name twice is two columns for one field too.
    targets = [mapped[name] for name in header if mapped[name] in COLUMNS]
    if len(targets) != len(set(targets)):
        raise ReviewError("Two columns in the CSV fill the same field (for example id and clip_id)")
    ignored = [name for name, target in mapped.items() if target not in COLUMNS and name]
    rows, problems, seen = [], [], set()
    for line, raw in enumerate(reader, start=2):
        if None in raw:
            problems.append(f"line {line}: more cells than columns")
            continue
        cells = {mapped[(k or "").strip()]: v for k, v in raw.items() if mapped.get((k or "").strip()) in COLUMNS}
        if not any((v or "").strip() for v in cells.values()):
            continue
        # A missing cell is not an empty one: saved as empty, it would wipe the stored value.
        if None in cells.values():
            problems.append(f"line {line}: fewer cells than columns")
            continue
        try:
            clip_id = clean_id(cells.pop("id") or "")
            if clip_id in seen:
                raise ReviewError(f"clip {clip_id} appears twice")
            seen.add(clip_id)
            rows.append({"id": clip_id, **clean_fields(cells)})
        except ReviewError as exc:
            problems.append(f"line {line}: {exc}")
    if problems:
        raise ReviewError("Nothing imported:\n  " + "\n  ".join(problems))
    added, updated = store.save_clips(rows)
    return added, updated, ignored


def export_csv(store: Store) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for clip in store.clips():
        writer.writerow({k: "" if clip[k] is None else clip[k] for k in COLUMNS})
    return out.getvalue()


def best_rating(clip: dict, performance: dict[str, ClipPerformance]) -> str:
    found = performance.get(clip["id"])
    return found.best if found else NO_DATA


def sort_key(clip: dict, row: dict | None, performance: dict[str, ClipPerformance]) -> tuple[int, int]:
    """Freshness first (redo, check, unknown, fresh, then not checked), then the clip's best
    rating on any platform (strong, typical, weak, no data). The listing and the dashboard both
    sort by this."""
    return freshness.rank(clip, row), RATINGS.index(best_rating(clip, performance))


def ordered(clips: list[dict], freshness_rows: dict[str, dict], performance: dict[str, ClipPerformance]) -> list[dict]:
    """Clips in listing order. The sort is stable: ties keep the order given (the store's, by id)."""
    return sorted(clips, key=lambda c: sort_key(c, freshness_rows.get(c["id"]), performance))


def rerecord_first(clip: dict, row: dict | None, performance: dict[str, ClipPerformance]) -> bool:
    """Its demo's UI changed (redo) and its posts did well (strong): the clip most worth re-recording."""
    return freshness.state(clip, row) == freshness.REDO and best_rating(clip, performance) == STRONG


def coverage(clips: list[dict]) -> tuple[list[str], list[str]]:
    """(demos with a current clip, demos without one). A demo is known once any clip names it."""
    demos = {c["demo"] for c in clips if c["demo"]}
    covered = {c["demo"] for c in clips if c["demo"] and c["status"] in CLIP_CURRENT}
    return sorted(covered), sorted(demos - covered)


@dataclass(frozen=True)
class View:
    """What the listing knows besides the clips themselves, read once."""

    store: Store
    freshness: dict[str, dict]
    now: datetime
    performance: dict[str, ClipPerformance] = field(default_factory=dict)  # by clip id; empty: not computed


# The listing's columns, left to right: (header, cell). A new column is one more entry.
Column = tuple[str, Callable[[dict, View], str]]
REPORT_COLUMNS: list[Column] = [
    ("clip", lambda c, v: c["id"]),
    ("demo", lambda c, v: c["demo"] or "-"),
    ("format", lambda c, v: c["format"]),
    ("status", lambda c, v: c["status"]),
    ("fresh", lambda c, v: freshness.label(c, v.freshness.get(c["id"]), v.now)),
    ("recorded", lambda c, v: c["recorded_at"][:10] or "-"),
    ("version", lambda c, v: c["project_version"] or "-"),
    ("posts", lambda c, v: str(len(v.store.clip_uses(c["id"])) or "-")),
    ("performance", lambda c, v: v.performance[c["id"]].summary() if c["id"] in v.performance else "-"),
    ("title", lambda c, v: c["title"]),
]


def report(
    store: Store,
    *,
    columns: list[Column] | None = None,
    now: datetime | None = None,
    performance: dict[str, ClipPerformance] | None = None,
) -> str:
    """The terminal view: one line per clip in the dashboard's order (the ones that need a look
    first, then by how they did), then why they need a look, then how each posted clip did, then
    coverage."""
    clips = store.clips()
    if not clips:
        return (
            "No clips yet. Add one with `argus-herald clips add <id> --demo ... --title ...`, or `clips import <csv>`."
        )
    view = View(store, store.clip_freshness(), now or datetime.now(UTC), performance or {})
    clips = ordered(clips, view.freshness, view.performance)
    columns = columns or REPORT_COLUMNS
    head = [name for name, _ in columns]
    body = [[cell(c, view) for _, cell in columns] for c in clips]
    widths = [max(len(row[i]) for row in (head, *body)) for i in range(len(head))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip() for row in (head, *body)]
    flagged = [c for c in clips if freshness.state(c, view.freshness.get(c["id"])) in freshness.FLAGGED]
    if flagged:
        lines.append("")
        lines.append("Needs a look (then `clips ack <id>` if it still matches, or `clips set <id> --status stale`):")
        for c in flagged:
            row = view.freshness.get(c["id"])
            why = freshness.explain(c, row, view.now)
            first = ", strong: re-record first" if rerecord_first(c, row, view.performance) else ""
            lines.append(f"  {c['id']} ({freshness.state(c, row)}{first}): {why[0] if why else ''}".rstrip())
            lines.extend(f"    {line}" for line in why[1:])
    posted = [c for c in clips if c["id"] in view.performance and view.performance[c["id"]].platforms]
    if posted:
        lines.append("")
        lines.append("Performance (each clip's median against the platform's own recent posts):")
        for c in posted:
            lines.append(f"  {c['id']}")
            lines.extend(f"    {line}" for line in view.performance[c["id"]].details())
    covered, missing = coverage(clips)
    total = len(covered) + len(missing)
    lines.append("")
    lines.append(
        f"Demos with a current clip: {len(covered)}/{total}" + (f". None yet: {', '.join(missing)}" if missing else "")
    )
    return "\n".join(lines)


def describe(store: Store, clip_id: str, performance: dict[str, ClipPerformance] | None = None) -> str:
    clip = require(store, clip_id)
    lines = [f"{key + ':':<17}{'' if clip[key] is None else clip[key]}" for key in COLUMNS]
    uses = store.clip_uses(clip["id"])
    lines.append("")
    lines.append(f"Used by {len(uses)} post(s)" + (":" if uses else "."))
    for use in uses:
        if use["via"] == "draft":
            lines.append(f"  draft #{use['id']:<4} {use['platform']:<12} {use['status']:<11} {use['ref']}".rstrip())
        else:
            lines.append(f"  logged #{use['id']:<3} {use['platform']:<12} {use['at'][:10]}  {use['ref']}".rstrip())
    row = store.clip_freshness().get(clip["id"])
    found = freshness.state(clip, row)
    lines.append("")
    if found:
        lines.append(f"Freshness: {found}")
        lines.extend(f"  {line}" for line in freshness.explain(clip, row))
    elif clip["status"] in CLIP_CURRENT:
        lines.append("Freshness: not checked yet (`argus-herald clips refresh`)")
    else:
        lines.append(f"Freshness: not checked while {clip['status']}")
    if performance is not None:
        found = performance.get(clip["id"])
        lines.append("")
        if found and found.platforms:
            lines.append("Performance (median against the platform's own recent posts):")
            lines.extend(f"  {line}" for line in found.details())
        else:
            lines.append("Performance: no data (no post that used it has gone out)")
    return "\n".join(lines)
