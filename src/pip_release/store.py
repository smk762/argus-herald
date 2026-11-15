"""SQLite queue of seen versions and drafts."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pip_release.models import DRAFT_FIELDS, Draft

_SCHEMA = """
-- Release sources: "pypi" (a PyPI project) or "github" (a repo's GitHub releases).
CREATE TABLE IF NOT EXISTS projects (
    source TEXT NOT NULL DEFAULT 'pypi',
    name TEXT NOT NULL,
    baselined_at TEXT NOT NULL,
    PRIMARY KEY (source, name)
);
CREATE TABLE IF NOT EXISTS seen (
    source TEXT NOT NULL DEFAULT 'pypi',
    project TEXT NOT NULL,
    version TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (source, project, version)
);
-- Files attached to a draft. `path` is the cleaned copy (metadata stripped); only it is posted.
CREATE TABLE IF NOT EXISTS draft_media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    alt TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL,
    removed TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_from TEXT NOT NULL UNIQUE,
    period_to TEXT NOT NULL,
    closes_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    site_path TEXT,
    site_hash TEXT,
    error TEXT
);
-- Releases collected in digest mode. digest_id is set when a window closes.
CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'pypi',
    project TEXT NOT NULL,
    version TEXT NOT NULL,
    released_at TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    excluded INTEGER NOT NULL DEFAULT 0,
    digest_id INTEGER REFERENCES digests(id),
    UNIQUE (source, project, version)
);
-- Posts made outside herald (Reddit, Hacker News, YouTube, newsletters). They never had a draft.
CREATE TABLE IF NOT EXISTS logged_broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'other',
    project TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
    reply TEXT NOT NULL DEFAULT '',
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
        self._migrate_sources()
        self.conn.executescript(_SCHEMA)
        self._migrate_drafts()
        self.conn.executescript(_DRAFTS.format(name="drafts"))
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(drafts)")}
        if "reply" not in columns:
            self.conn.execute("ALTER TABLE drafts ADD COLUMN reply TEXT NOT NULL DEFAULT ''")

    def _migrate_sources(self) -> None:
        """Add a source column to projects and seen (keys were PyPI names only)."""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(seen)")}
        if not columns or "source" in columns:
            return
        with self.transaction():
            self.conn.execute("ALTER TABLE projects RENAME TO projects_old")
            self.conn.execute("ALTER TABLE seen RENAME TO seen_old")
            self.conn.execute(
                "CREATE TABLE projects (source TEXT NOT NULL DEFAULT 'pypi', name TEXT NOT NULL, "
                "baselined_at TEXT NOT NULL, PRIMARY KEY (source, name))"
            )
            self.conn.execute(
                "CREATE TABLE seen (source TEXT NOT NULL DEFAULT 'pypi', project TEXT NOT NULL, version TEXT NOT NULL, "
                "seen_at TEXT NOT NULL, PRIMARY KEY (source, project, version))"
            )
            self.conn.execute("INSERT INTO projects (source, name, baselined_at) SELECT 'pypi', name, baselined_at FROM projects_old")
            self.conn.execute("INSERT INTO seen (source, project, version, seen_at) SELECT 'pypi', project, version, seen_at FROM seen_old")
            self.conn.execute("DROP TABLE projects_old")
            self.conn.execute("DROP TABLE seen_old")

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

    def is_baselined(self, project: str, source: str = "pypi") -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM projects WHERE source = ? AND name = ?", (source, project)
        ).fetchone()
        return row is not None

    def baseline(self, project: str, versions: list[str], source: str = "pypi") -> None:
        now = _now()
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO projects (source, name, baselined_at) VALUES (?, ?, ?)",
                (source, project, now),
            )
            self.conn.executemany(
                "INSERT OR IGNORE INTO seen (source, project, version, seen_at) VALUES (?, ?, ?, ?)",
                [(source, project, version, now) for version in versions],
            )

    def seen(self, project: str, version: str, source: str = "pypi") -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen WHERE source = ? AND project = ? AND version = ?",
            (source, project, version),
        ).fetchone()
        return row is not None

    def mark_seen(self, project: str, version: str, source: str = "pypi") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen (source, project, version, seen_at) VALUES (?, ?, ?, ?)",
            (source, project, version, _now()),
        )

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
        reply: str = "",
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
                kind, post_key, subject, project, version, platform, title, body, link, reply,
                status, edited, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (kind, post_key, subject, project, version, platform, title, body, link, reply, now, now),
        )
        self.conn.commit()
        if cur.rowcount:
            return int(cur.lastrowid), True
        row = self.conn.execute(
            "SELECT id FROM drafts WHERE post_key = ? AND platform = ?", (post_key, platform)
        ).fetchone()
        return int(row["id"]), False

    # ---- digest mode -------------------------------------------------------------------

    def add_release(
        self, *, project: str, version: str, released_at: str, summary: str, notes: str, url: str,
        source: str = "pypi",
    ) -> bool:
        """Record a release for the digest. A version on both PyPI and GitHub is kept once, under
        pypi, with the GitHub notes. Returns True when a new row was added."""
        other = "github" if source == "pypi" else "pypi" if source == "github" else None
        twin = self.release(project, version, other) if other else None
        if twin is not None and source == "github":
            if notes.strip():
                self.conn.execute("UPDATE releases SET notes = ? WHERE id = ?", (notes, twin["id"]))
            return False
        if twin is not None and source == "pypi":
            if twin["digest_id"] is not None:
                return False  # already announced as a GitHub release in a closed digest
            notes = twin["notes"] if twin["notes"].strip() else notes
            released_at = min(released_at or twin["released_at"], twin["released_at"])
            self.conn.execute("DELETE FROM releases WHERE id = ?", (twin["id"],))
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO releases (source, project, version, released_at, seen_at, summary, notes, url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source, project, version, released_at or _now(), _now(), summary, notes, url),
        )
        return bool(cur.rowcount)

    def releases(self, *, digest_id: int | None = None, open_only: bool = False) -> list[sqlite3.Row]:
        """Releases of one digest, or those not in any digest yet (open_only), oldest first."""
        if open_only:
            where, params = "digest_id IS NULL", ()
        elif digest_id is not None:
            where, params = "digest_id = ?", (digest_id,)
        else:
            where, params = "1", ()
        return self.conn.execute(
            f"SELECT * FROM releases WHERE {where} ORDER BY released_at, id", params
        ).fetchall()

    def release(self, project: str, version: str, source: str = "pypi") -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM releases WHERE source = ? AND project = ? AND version = ?", (source, project, version)
        ).fetchone()

    def set_excluded(self, release_id: int, excluded: bool) -> None:
        self.conn.execute("UPDATE releases SET excluded = ? WHERE id = ?", (int(excluded), release_id))

    def seen_versions(self, project: str, source: str | None = None) -> list[str]:
        """Versions seen for a project, from one source or (default) any."""
        if source is None:
            rows = self.conn.execute("SELECT DISTINCT version FROM seen WHERE project = ?", (project,))
        else:
            rows = self.conn.execute("SELECT version FROM seen WHERE source = ? AND project = ?", (source, project))
        return [row["version"] for row in rows]

    def create_digest(self, period_from: str, period_to: str, closes_at: str) -> int:
        """A digest for one window. Takes every open release seen before the window closed."""
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO digests (period_from, period_to, closes_at, created_at) VALUES (?, ?, ?, ?)",
                (period_from, period_to, closes_at, _now()),
            )
            digest_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE releases SET digest_id = ? WHERE digest_id IS NULL AND released_at < ?",
                (digest_id, closes_at),
            )
        return digest_id

    def digests(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM digests ORDER BY period_from DESC").fetchall()

    def digest(self, digest_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()

    def update_digest(self, digest_id: int, **fields: object) -> None:
        allowed = {"site_path", "site_hash", "error"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"Unknown digest fields: {set(fields) - allowed}")
        sets = ", ".join(f"{key} = ?" for key in fields)
        self.conn.execute(f"UPDATE digests SET {sets} WHERE id = ?", (*fields.values(), digest_id))

    def refresh_text(self, draft_id: int, title: str, body: str, reply: str = "") -> bool:
        """Replace a pending draft's text only if a person hasn't edited it."""
        cur = self.conn.execute(
            "UPDATE drafts SET title = ?, body = ?, reply = ?, updated_at = ? "
            "WHERE id = ? AND status = 'pending' AND edited = 0",
            (title, body, reply, _now(), draft_id),
        )
        return bool(cur.rowcount)

    # ---- broadcasts made outside herald ------------------------------------------------

    _LOGGED = ("platform", "url", "kind", "project", "subject", "text", "at")

    def log_broadcast(self, **fields: str) -> int:
        if set(fields) != set(self._LOGGED):
            raise ValueError(f"log_broadcast needs exactly {self._LOGGED}")
        now = _now()
        try:
            cur = self.conn.execute(
                f"INSERT INTO logged_broadcasts ({', '.join(self._LOGGED)}, created_at, updated_at) "
                f"VALUES ({', '.join('?' for _ in self._LOGGED)}, ?, ?)",
                (*(fields[k] for k in self._LOGGED), now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"{fields['url']} is already logged") from exc
        return int(cur.lastrowid)

    def logged_broadcasts(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM logged_broadcasts ORDER BY at DESC, id DESC").fetchall()

    def logged_broadcast(self, broadcast_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM logged_broadcasts WHERE id = ?", (broadcast_id,)).fetchone()

    def update_logged(self, broadcast_id: int, **fields: str) -> bool:
        unknown = set(fields) - set(self._LOGGED)
        if unknown or not fields:
            raise ValueError(f"Unknown fields: {unknown}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        try:
            cur = self.conn.execute(
                f"UPDATE logged_broadcasts SET {sets}, updated_at = ? WHERE id = ?",
                (*fields.values(), _now(), broadcast_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"{fields.get('url')} is already logged") from exc
        return bool(cur.rowcount)

    def delete_logged(self, broadcast_id: int) -> bool:
        return bool(self.conn.execute("DELETE FROM logged_broadcasts WHERE id = ?", (broadcast_id,)).rowcount)

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

    def add_media(self, draft_id: int, *, kind: str, path: str, name: str, alt: str, size: int, removed: str) -> int:
        position = self.conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM draft_media WHERE draft_id = ?", (draft_id,)
        ).fetchone()[0]
        cur = self.conn.execute(
            "INSERT INTO draft_media (draft_id, position, kind, path, name, alt, bytes, removed, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (draft_id, position, kind, path, name, alt, size, removed, _now()),
        )
        return int(cur.lastrowid)

    def media(self, draft_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM draft_media WHERE draft_id = ? ORDER BY position, id", (draft_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def media_item(self, media_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM draft_media WHERE id = ?", (media_id,)).fetchone()
        return dict(row) if row else None

    def set_alt(self, media_id: int, alt: str) -> bool:
        return bool(self.conn.execute("UPDATE draft_media SET alt = ? WHERE id = ?", (alt, media_id)).rowcount)

    def remove_media(self, media_id: int) -> bool:
        return bool(self.conn.execute("DELETE FROM draft_media WHERE id = ?", (media_id,)).rowcount)

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
        self, draft_id: int, title: str, body: str, from_statuses: tuple[str, ...], reply: str | None = None
    ) -> Draft | None:
        fields: dict[str, object] = {"title": title, "body": body, "edited": 1, "error": None}
        if reply is not None:
            fields["reply"] = reply
        return self.transition(draft_id, from_statuses, "pending", **fields)


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
        reply=row["reply"],
    )
