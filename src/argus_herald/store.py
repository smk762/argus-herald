"""SQLite queue of seen versions and drafts."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from argus_cortex.timestamps import utc_now_iso

from argus_herald.models import CLIP_FIELDS, DRAFT_FIELDS, Draft

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
-- Demo clips: what each one shows and what it was recorded against. `path` is the edited file.
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    demo TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    format TEXT NOT NULL DEFAULT 'clip',
    target_seconds INTEGER,
    status TEXT NOT NULL DEFAULT 'todo',
    recorded_at TEXT NOT NULL DEFAULT '',
    project_version TEXT NOT NULL DEFAULT '',
    demo_repo TEXT NOT NULL DEFAULT '',
    demo_commit TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    stale_if TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- The last freshness check of each clip (see freshness.py). base_* is what the clip was compared
-- from; when the clip's own fields no longer match, the row is out of date. A failed check keeps
-- the last result and fills failed_at and error.
CREATE TABLE IF NOT EXISTS clip_freshness (
    clip_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    commits INTEGER NOT NULL DEFAULT 0,
    ui_files TEXT NOT NULL DEFAULT '[]',
    files_changed INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    head_commit TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    base_commit TEXT NOT NULL DEFAULT '',
    base_version TEXT NOT NULL DEFAULT '',
    newest_version TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL DEFAULT '',
    failed_at TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
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
-- Posts of mint teasers to report back to argus-mint (see mint.py). A row is written when the
-- post is known to be live; `at` is fixed then, so a retry sends the same report. reported_at is
-- filled when mint has it; refused is 1 when mint turned it down for good (error says why).
CREATE TABLE IF NOT EXISTS mint_reports (
    draft_id INTEGER PRIMARY KEY REFERENCES drafts(id) ON DELETE CASCADE,
    mint_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    url TEXT,
    at TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    reported_at TEXT NOT NULL DEFAULT '',
    refused INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
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
    drafted_by TEXT NOT NULL DEFAULT 'template',
    publish_at TEXT,
    UNIQUE (post_key, platform)
);
"""


def release_key(project: str, version: str) -> str:
    return f"release:{project}=={version}"


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
        if "drafted_by" not in columns:
            self.conn.execute("ALTER TABLE drafts ADD COLUMN drafted_by TEXT NOT NULL DEFAULT 'template'")
        if "publish_at" not in columns:
            self.conn.execute("ALTER TABLE drafts ADD COLUMN publish_at TEXT")
        # Which registry clip an attachment or a hand-logged post used (NULL: none).
        for table in ("draft_media", "logged_broadcasts"):
            if "clip_id" not in {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN clip_id TEXT")

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
            self.conn.execute(
                "INSERT INTO projects (source, name, baselined_at) SELECT 'pypi', name, baselined_at FROM projects_old"
            )
            self.conn.execute(
                "INSERT INTO seen (source, project, version, seen_at) SELECT 'pypi', project, version, seen_at FROM seen_old"
            )
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
        row = self.conn.execute("SELECT 1 FROM projects WHERE source = ? AND name = ?", (source, project)).fetchone()
        return row is not None

    def baseline(self, project: str, versions: list[str], source: str = "pypi") -> None:
        now = utc_now_iso()
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
            (source, project, version, utc_now_iso()),
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
        drafted_by: str = "template",
    ) -> tuple[int, bool]:
        """Insert a pending draft. Returns (id, created); an existing (post_key, platform) is kept."""
        if post_key is None:
            if not (project and version):
                raise ValueError("A draft needs a post_key, or a project and version")
            post_key = release_key(project, version)
        now = utc_now_iso()
        # Only the (post_key, platform) conflict means "already drafted"; any other constraint
        # still raises. No commit here: outside a transaction the insert commits on its own, and
        # inside one (compose) the caller commits every destination's draft together.
        cur = self.conn.execute(
            """
            INSERT INTO drafts (
                kind, post_key, subject, project, version, platform, title, body, link, reply, drafted_by,
                status, edited, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            ON CONFLICT (post_key, platform) DO NOTHING
            """,
            (kind, post_key, subject, project, version, platform, title, body, link, reply, drafted_by, now, now),
        )
        if cur.rowcount:
            return int(cur.lastrowid), True
        row = self.conn.execute(
            "SELECT id FROM drafts WHERE post_key = ? AND platform = ?", (post_key, platform)
        ).fetchone()
        return int(row["id"]), False

    # ---- digest mode -------------------------------------------------------------------

    def add_release(
        self,
        *,
        project: str,
        version: str,
        released_at: str,
        summary: str,
        notes: str,
        url: str,
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
            # One statement turns the GitHub row into the PyPI one: nothing is lost if the process
            # stops here, and what a person set on it (excluded) stays.
            cur = self.conn.execute(
                "UPDATE OR IGNORE releases SET source = ?, released_at = ?, summary = ?, notes = ?, url = ? "
                "WHERE id = ?",
                (source, released_at, summary, notes, url, twin["id"]),
            )
            return bool(cur.rowcount)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO releases (source, project, version, released_at, seen_at, summary, notes, url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source, project, version, released_at or utc_now_iso(), utc_now_iso(), summary, notes, url),
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
        return self.conn.execute(f"SELECT * FROM releases WHERE {where} ORDER BY released_at, id", params).fetchall()

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

    def seen_versions_named(self, project: str) -> list[str]:
        """Versions seen for a project from any source, matching its name the way PyPI does
        (case, and '-', '_' and '.' alike)."""
        from packaging.utils import canonicalize_name

        key = canonicalize_name(project)
        rows = self.conn.execute("SELECT DISTINCT project, version FROM seen")
        return [row["version"] for row in rows if canonicalize_name(row["project"]) == key]

    def create_digest(self, period_from: str, period_to: str, closes_at: str) -> int:
        """A digest for one window. Takes every open release seen before the window closed."""
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO digests (period_from, period_to, closes_at, created_at) VALUES (?, ?, ?, ?)",
                (period_from, period_to, closes_at, utc_now_iso()),
            )
            digest_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE releases SET digest_id = ? WHERE digest_id IS NULL AND released_at < ?",
                (digest_id, closes_at),
            )
        return digest_id

    def drop_digest(self, digest_id: int) -> None:
        """Undo create_digest: its releases are open again, so the window closes on the next poll."""
        with self.transaction():
            self.conn.execute("UPDATE releases SET digest_id = NULL WHERE digest_id = ?", (digest_id,))
            self.conn.execute("DELETE FROM digests WHERE id = ?", (digest_id,))

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

    def refresh_text(
        self,
        draft_id: int,
        title: str,
        body: str,
        reply: str = "",
        drafted_by: str = "template",
        note: str | None = None,
        *,
        force: bool = False,
        expect: Draft | None = None,
    ) -> bool:
        """Replace a pending draft's text only if a person hasn't edited it (or force). With expect,
        only if the draft is still as expect read it: the same text, and not touched since (a draft
        that was approved or withdrawn and came back to pending has a newer updated_at)."""
        conditions = "" if force else " AND edited = 0"
        params: list[object] = [title, body, reply, drafted_by, note, utc_now_iso(), draft_id]
        if expect is not None:
            conditions += " AND title = ? AND body = ? AND reply = ? AND edited = ? AND updated_at = ?"
            params += [expect.title, expect.body, expect.reply, int(expect.edited), expect.updated_at]
        cur = self.conn.execute(
            "UPDATE drafts SET title = ?, body = ?, reply = ?, drafted_by = ?, note = ?, edited = 0, updated_at = ? "
            f"WHERE id = ? AND status = 'pending'{conditions}",
            params,
        )
        return bool(cur.rowcount)

    # ---- broadcasts made outside herald ------------------------------------------------

    _LOGGED = ("platform", "url", "kind", "project", "subject", "text", "at")

    def log_broadcast(self, **fields: str) -> int:
        if set(fields) != set(self._LOGGED):
            raise ValueError(f"log_broadcast needs exactly {self._LOGGED}")
        now = utc_now_iso()
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
                (*fields.values(), utc_now_iso(), broadcast_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"{fields.get('url')} is already logged") from exc
        return bool(cur.rowcount)

    def delete_logged(self, broadcast_id: int) -> bool:
        return bool(self.conn.execute("DELETE FROM logged_broadcasts WHERE id = ?", (broadcast_id,)).rowcount)

    def set_logged_clip(self, broadcast_id: int, clip_id: str | None) -> bool:
        cur = self.conn.execute(
            "UPDATE logged_broadcasts SET clip_id = ?, updated_at = ? WHERE id = ?",
            (clip_id, utc_now_iso(), broadcast_id),
        )
        return bool(cur.rowcount)

    # ---- clip registry -----------------------------------------------------------------

    def clips(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM clips ORDER BY id")]

    def clip(self, clip_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        return dict(row) if row else None

    def save_clips(self, rows: list[dict]) -> tuple[int, int]:
        """Add or update clips in one transaction; each row carries `id` and the fields to set.
        Returns (added, updated)."""
        now = utc_now_iso()
        added = updated = 0
        with self.transaction():
            for row in rows:
                fields = {k: v for k, v in row.items() if k != "id"}
                if set(fields) - set(CLIP_FIELDS):
                    raise ValueError(f"Unknown clip fields: {set(fields) - set(CLIP_FIELDS)}")
                if self.conn.execute("SELECT 1 FROM clips WHERE id = ?", (row["id"],)).fetchone():
                    sets = "".join(f"{key} = ?, " for key in fields)
                    self.conn.execute(
                        f"UPDATE clips SET {sets}updated_at = ? WHERE id = ?", (*fields.values(), now, row["id"])
                    )
                    updated += 1
                else:
                    names = "".join(f"{key}, " for key in fields)
                    self.conn.execute(
                        f"INSERT INTO clips (id, {names}created_at, updated_at) VALUES (?, {'?, ' * len(fields)}?, ?)",
                        (row["id"], *fields.values(), now, now),
                    )
                    added += 1
        return added, updated

    def delete_clip(self, clip_id: str) -> bool:
        with self.transaction():
            self.conn.execute("DELETE FROM clip_freshness WHERE clip_id = ?", (clip_id,))
            return bool(self.conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,)).rowcount)

    def clip_freshness(self) -> dict[str, dict]:
        """The last freshness check of every clip that has one, by clip id."""
        return {row["clip_id"]: dict(row) for row in self.conn.execute("SELECT * FROM clip_freshness")}

    def save_freshness(self, clip_id: str, **fields: object) -> None:
        """Record a completed check; it replaces the last one, failure included."""
        row = {"clip_id": clip_id, **fields, "checked_at": utc_now_iso(), "failed_at": "", "error": ""}
        names = ", ".join(row)
        self.conn.execute(
            f"INSERT OR REPLACE INTO clip_freshness ({names}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )

    def freshness_failed(self, clip_id: str, error: str, *, base_commit: str, base_version: str) -> None:
        """Record a check that couldn't complete. The last result is kept as it was, unless it was
        for another commit or version of the clip (then there is no last result)."""
        with self.transaction():
            self.conn.execute(
                "DELETE FROM clip_freshness WHERE clip_id = ? AND (base_commit != ? OR base_version != ?)",
                (clip_id, base_commit, base_version),
            )
            self.conn.execute(
                "INSERT INTO clip_freshness (clip_id, state, base_commit, base_version, failed_at, error) "
                "VALUES (?, 'unknown', ?, ?, ?, ?) "
                "ON CONFLICT(clip_id) DO UPDATE SET failed_at = excluded.failed_at, error = excluded.error",
                (clip_id, base_commit, base_version, utc_now_iso(), error[:300]),
            )

    def clip_uses(self, clip_id: str) -> list[dict]:
        """Every post that used a clip: drafts it is attached to, then hand-logged broadcasts."""
        drafts = self.conn.execute(
            "SELECT DISTINCT 'draft' AS via, d.id AS id, d.platform AS platform, d.status AS status, "
            "d.updated_at AS at, COALESCE(d.external_id, '') AS ref "
            "FROM draft_media m JOIN drafts d ON d.id = m.draft_id WHERE m.clip_id = ? ORDER BY d.id",
            (clip_id,),
        ).fetchall()
        logged = self.conn.execute(
            "SELECT 'logged' AS via, id, platform, 'posted' AS status, at, url AS ref FROM logged_broadcasts "
            "WHERE clip_id = ? ORDER BY at, id",
            (clip_id,),
        ).fetchall()
        return [dict(row) for row in (*drafts, *logged)]

    def clip_links(self) -> list[tuple[str, str, int]]:
        """Every (clip id, 'draft' or 'logged', draft or broadcast id) link, for all clips at once."""
        rows = self.conn.execute(
            "SELECT DISTINCT clip_id, 'draft', draft_id FROM draft_media WHERE clip_id IS NOT NULL "
            "UNION SELECT clip_id, 'logged', id FROM logged_broadcasts WHERE clip_id IS NOT NULL"
        )
        return [(str(clip_id), via, int(ref)) for clip_id, via, ref in rows]

    def feed_baselined(self, source: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM feed_sources WHERE source = ?", (source,)).fetchone()
        return row is not None

    def baseline_feed(self, source: str, item_ids: list[str]) -> None:
        now = utc_now_iso()
        with self.transaction():
            self.conn.execute("INSERT OR IGNORE INTO feed_sources (source, baselined_at) VALUES (?, ?)", (source, now))
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
            (source, item_id, utc_now_iso()),
        )

    def add_mint_report(
        self, draft_id: int, *, mint_id: str, platform: str, url: str | None, at: str, sha256: str
    ) -> bool:
        """Queue one report per draft. False when the draft already has one."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO mint_reports (draft_id, mint_id, platform, url, at, sha256) VALUES (?, ?, ?, ?, ?, ?)",
            (draft_id, mint_id, platform, url, at, sha256),
        )
        return cur.rowcount == 1

    def mint_report(self, draft_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM mint_reports WHERE draft_id = ?", (draft_id,)).fetchone()
        return dict(row) if row else None

    def mint_reports_due(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM mint_reports WHERE reported_at = '' AND refused = 0 ORDER BY draft_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def settle_mint_report(self, draft_id: int, *, reported: bool, refused: bool = False, error: str = "") -> None:
        self.conn.execute(
            "UPDATE mint_reports SET reported_at = ?, refused = ?, error = ? WHERE draft_id = ?",
            (utc_now_iso() if reported else "", int(refused), error[:500], draft_id),
        )

    def get(self, draft_id: int) -> Draft | None:
        row = self.conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return _draft(row) if row else None

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
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
        rows = self.conn.execute(f"SELECT * FROM drafts {where} ORDER BY id", params).fetchall()
        return [_draft(row) for row in rows]

    def transition(
        self,
        draft_id: int,
        from_statuses: tuple[str, ...],
        to_status: str,
        *,
        expect: Mapping[str, object] | None = None,
        **fields: object,
    ) -> Draft | None:
        """Move a draft from one of from_statuses to to_status, setting fields. None when it wasn't
        in one of those statuses, or when a column named in expect no longer holds the value given
        (None: still empty). The check and the write are one statement, so a caller can act on what
        it read without another request changing the draft in between."""
        assignments = ["status = ?", "updated_at = ?"]
        params: list[object] = [to_status, utc_now_iso()]
        if to_status == "pending" and "publish_at" not in fields:
            fields = {**fields, "publish_at": None}  # back to review: its slot is released
        for key, value in fields.items():
            if key not in DRAFT_FIELDS:
                raise ValueError(f"Unknown draft field {key}")
            assignments.append(f"{key} = ?")
            params.append(value)
        marks = ", ".join("?" for _ in from_statuses)
        params.extend([draft_id, *from_statuses])
        conditions = ""
        for key, value in (expect or {}).items():
            if key not in DRAFT_FIELDS and key != "updated_at":
                raise ValueError(f"Unknown draft field {key}")
            if value is None:
                conditions += f" AND {key} IS NULL"
            else:
                conditions += f" AND {key} = ?"
                params.append(value)
        cur = self.conn.execute(
            f"""
            UPDATE drafts SET {", ".join(assignments)}
            WHERE id = ? AND status IN ({marks}){conditions}
            """,
            params,
        )
        # No commit here: autocommit already ends a lone statement, and a caller's transaction()
        # must be able to roll this back with the rest of its writes.
        if cur.rowcount != 1:
            return None
        return self.get(draft_id)

    def add_media(
        self,
        draft_id: int,
        *,
        kind: str,
        path: str,
        name: str,
        alt: str,
        size: int,
        removed: str,
        clip_id: str | None = None,
    ) -> int:
        position = self.conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM draft_media WHERE draft_id = ?", (draft_id,)
        ).fetchone()[0]
        cur = self.conn.execute(
            "INSERT INTO draft_media (draft_id, position, kind, path, name, alt, bytes, removed, created_at, clip_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (draft_id, position, kind, path, name, alt, size, removed, utc_now_iso(), clip_id),
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

    def set_publish_at(self, draft_id: int, when: str | None) -> bool:
        cur = self.conn.execute(
            "UPDATE drafts SET publish_at = ?, updated_at = ? WHERE id = ? AND status = 'approved'",
            (when, utc_now_iso(), draft_id),
        )
        return bool(cur.rowcount)

    def due(self, now: str) -> list[Draft]:
        rows = self.conn.execute(
            "SELECT * FROM drafts WHERE status = 'approved' AND publish_at IS NOT NULL AND publish_at <= ? "
            "ORDER BY publish_at, id",
            (now,),
        ).fetchall()
        return [_draft(row) for row in rows]

    def scheduled(self, platform: str | None = None) -> list[Draft]:
        clause, params = ("AND platform = ?", (platform,)) if platform else ("", ())
        rows = self.conn.execute(
            f"SELECT * FROM drafts WHERE status = 'approved' AND publish_at IS NOT NULL {clause} ORDER BY publish_at",
            params,
        ).fetchall()
        return [_draft(row) for row in rows]

    def post_times(self, platform: str) -> list[str]:
        """When this platform's posts went out (or were handed off to be posted)."""
        rows = self.conn.execute(
            "SELECT updated_at AS t FROM drafts WHERE platform = ? AND status IN ('published', 'handed_off')",
            (platform,),
        ).fetchall()
        return [row["t"] for row in rows]

    def set_note(self, draft_id: int, note: str | None) -> None:
        self.conn.execute("UPDATE drafts SET note = ? WHERE id = ?", (note, draft_id))

    def accept_hit(self, draft_id: int, key: str, note: str) -> None:
        self.conn.execute(
            "INSERT INTO gate_accepts (draft_id, key, note, accepted_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(draft_id, key) DO UPDATE SET note = excluded.note, accepted_at = excluded.accepted_at",
            (draft_id, key, note, utc_now_iso()),
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
        now = utc_now_iso()
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
        drafted_by=row["drafted_by"],
        publish_at=row["publish_at"],
    )
