"""SQLite queue of seen versions and drafts."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pip_release.models import DRAFT_FIELDS, Draft

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    baselined_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
    project TEXT NOT NULL,
    version TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (project, version)
);
CREATE TABLE IF NOT EXISTS gate_accepts (
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    note TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    PRIMARY KEY (draft_id, key)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_sources (
    source TEXT PRIMARY KEY,
    baselined_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_items (
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (source, item_id)
);
"""

# One row per draft. `post_key` ties the per-platform drafts of one piece of content together:
# "release:<project>==<version>", "post:<id>" for composed posts, "<source>:<item id>" for feeds.
# Releases fill project and version; other kinds leave them empty and use `subject`.
_DRAFTS = """
CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'release',
    post_key TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    link TEXT NOT NULL,
    status TEXT NOT NULL,
    edited INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    note TEXT,
    external_id TEXT,
    error TEXT,
    UNIQUE (post_key, platform)
);
"""


def release_key(project: str, version: str) -> str:
    return f"release:{project}=={version}"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_SCHEMA)
        self._migrate_drafts()
        self.conn.executescript(_DRAFTS.format(name="drafts"))

    def _migrate_drafts(self) -> None:
        """Rebuild a drafts table from before draft kinds (UNIQUE project, version, platform)."""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(drafts)")}
        if not columns or "post_key" in columns:
            return
        # Dropping the old table must not cascade into gate_accepts.
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.transaction():
                self.conn.execute(_DRAFTS.format(name="drafts_new"))
                self.conn.execute(
                    """
                    INSERT INTO drafts_new (
                        id, kind, post_key, subject, project, version, platform, title, body,
                        link, status, edited, created_at, updated_at, note, external_id, error
                    )
                    SELECT id, 'release', 'release:' || project || '==' || version, '', project,
                        version, platform, title, body, link, status, edited, created_at,
                        updated_at, note, external_id, error
                    FROM drafts
                    """
                )
                self.conn.execute("DROP TABLE drafts")
                self.conn.execute("ALTER TABLE drafts_new RENAME TO drafts")
                bad = self.conn.execute("PRAGMA foreign_key_check").fetchall()
                if bad:
                    raise sqlite3.IntegrityError(f"foreign key check failed after migration: {len(bad)} rows")
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN")
        try:
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def is_baselined(self, project: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM projects WHERE name = ?", (project,)
        ).fetchone()
        return row is not None

    def baseline(self, project: str, versions: list[str]) -> None:
        now = _now()
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO projects (name, baselined_at) VALUES (?, ?)",
                (project, now),
            )
            self.conn.executemany(
                "INSERT OR IGNORE INTO seen (project, version, seen_at) VALUES (?, ?, ?)",
                [(project, version, now) for version in versions],
            )

    def seen(self, project: str, version: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen WHERE project = ? AND version = ?",
            (project, version),
        ).fetchone()
        return row is not None

    def mark_seen(self, project: str, version: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen (project, version, seen_at) VALUES (?, ?, ?)",
            (project, version, _now()),
        )
        self.conn.commit()

    def add_draft(
        self,
        *,
        platform: str,
        title: str,
        body: str,
        link: str,
        project: str = "",
        version: str = "",
        kind: str = "release",
        post_key: str | None = None,
        subject: str = "",
    ) -> tuple[int, bool]:
        """Insert a pending draft. Returns (id, created); an existing (post_key, platform) is kept."""
        if post_key is None:
            if not (project and version):
                raise ValueError("A draft needs a post_key, or a project and version")
            post_key = release_key(project, version)
        now = _now()
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO drafts (
                kind, post_key, subject, project, version, platform, title, body, link,
                status, edited, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (kind, post_key, subject, project, version, platform, title, body, link, now, now),
        )
        self.conn.commit()
        if cur.rowcount:
            return int(cur.lastrowid), True
        row = self.conn.execute(
            "SELECT id FROM drafts WHERE post_key = ? AND platform = ?", (post_key, platform)
        ).fetchone()
        return int(row["id"]), False

    def feed_baselined(self, source: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM feed_sources WHERE source = ?", (source,)).fetchone()
        return row is not None

    def baseline_feed(self, source: str, item_ids: list[str]) -> None:
        now = _now()
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO feed_sources (source, baselined_at) VALUES (?, ?)", (source, now)
            )
            self.conn.executemany(
                "INSERT OR IGNORE INTO feed_items (source, item_id, seen_at) VALUES (?, ?, ?)",
                [(source, item, now) for item in item_ids],
            )

    def feed_seen(self, source: str, item_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM feed_items WHERE source = ? AND item_id = ?", (source, item_id)
        ).fetchone()
        return row is not None

    def mark_feed_seen(self, source: str, item_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO feed_items (source, item_id, seen_at) VALUES (?, ?, ?)",
            (source, item_id, _now()),
        )

    def get(self, draft_id: int) -> Draft | None:
        row = self.conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return _draft(row) if row else None

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def list_drafts(
        self,
        *,
        status: str | None = None,
        project: str | None = None,
        version: str | None = None,
        post_key: str | None = None,
    ) -> list[Draft]:
        clauses: list[str] = []
        params: list[object] = []
        if post_key:
            clauses.append("post_key = ?")
            params.append(post_key)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if project:
            clauses.append("project = ?")
            params.append(project)
        if version:
            clauses.append("version = ?")
            params.append(version)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM drafts {where} ORDER BY id", params
        ).fetchall()
        return [_draft(row) for row in rows]

    def transition(
        self,
        draft_id: int,
        from_statuses: tuple[str, ...],
        to_status: str,
        **fields: object,
    ) -> Draft | None:
        assignments = ["status = ?", "updated_at = ?"]
        params: list[object] = [to_status, _now()]
        for key, value in fields.items():
            if key not in DRAFT_FIELDS:
                raise ValueError(f"Unknown draft field {key}")
            assignments.append(f"{key} = ?")
            params.append(value)
        marks = ", ".join("?" for _ in from_statuses)
        params.extend([draft_id, *from_statuses])
        cur = self.conn.execute(
            f"""
            UPDATE drafts SET {", ".join(assignments)}
            WHERE id = ? AND status IN ({marks})
            """,
            params,
        )
        self.conn.commit()
        if cur.rowcount != 1:
            return None
        return self.get(draft_id)

    def accept_hit(self, draft_id: int, key: str, note: str) -> None:
        self.conn.execute(
            "INSERT INTO gate_accepts (draft_id, key, note, accepted_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(draft_id, key) DO UPDATE SET note = excluded.note, accepted_at = excluded.accepted_at",
            (draft_id, key, note, _now()),
        )

    def accepted(self, draft_id: int) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT key, note FROM gate_accepts WHERE draft_id = ? ORDER BY accepted_at", (draft_id,)
        ).fetchall()
        return {row["key"]: row["note"] for row in rows}

    def pending_before(self, before: str | None = None) -> list[Draft]:
        """Pending drafts, optionally only those created before an ISO date or timestamp."""
        if before is None:
            return self.list_drafts(status="pending")
        rows = self.conn.execute(
            "SELECT * FROM drafts WHERE status = 'pending' AND created_at < ? ORDER BY id",
            (before,),
        ).fetchall()
        return [_draft(row) for row in rows]

    def reject_pending(self, ids: list[int], note: str) -> int:
        """Reject the given drafts in one transaction. Rows no longer pending are left alone."""
        if not ids:
            return 0
        now = _now()
        changed = 0
        with self.transaction():
            for draft_id in ids:
                cur = self.conn.execute(
                    "UPDATE drafts SET status = 'rejected', note = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (note, now, draft_id),
                )
                changed += cur.rowcount
        return changed

    def update_text(
        self, draft_id: int, title: str, body: str, from_statuses: tuple[str, ...]
    ) -> Draft | None:
        return self.transition(
            draft_id,
            from_statuses,
            "pending",
            title=title,
            body=body,
            edited=1,
            error=None,
        )


def _draft(row: sqlite3.Row) -> Draft:
    return Draft(
        id=int(row["id"]),
        project=row["project"],
        version=row["version"],
        platform=row["platform"],
        title=row["title"],
        body=row["body"],
        link=row["link"],
        status=row["status"],
        edited=bool(row["edited"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        note=row["note"],
        external_id=row["external_id"],
        error=row["error"],
        kind=row["kind"],
        post_key=row["post_key"],
        subject=row["subject"],
    )
