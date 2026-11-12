"""SQLite storage: connection handling, schema, and migration.

Design rules enforced here:

* Raw captures are immutable.  Triggers reject UPDATE, and DELETE is rejected
  unless the user has explicitly armed deletion through ``system_flags``.
* The Markdown files are the durable human-readable artifact; SQLite is the
  queryable index over them.
* Every table stores UTC ISO-8601 timestamps so lexical ordering is valid.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS system_flags (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_captures (
    id                   TEXT PRIMARY KEY,
    chat_id              TEXT NOT NULL,
    message_id           INTEGER,
    received_at          TEXT NOT NULL,
    source               TEXT NOT NULL,
    language             TEXT NOT NULL,
    text                 TEXT NOT NULL,
    text_format          TEXT NOT NULL DEFAULT 'text',
    entities_json        TEXT NOT NULL DEFAULT '[]',
    media_json           TEXT NOT NULL DEFAULT '[]',
    reply_to_id          TEXT,
    conversation_context TEXT,
    content_hash         TEXT NOT NULL,
    path                 TEXT NOT NULL,
    imported             INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_hash_v2 ON raw_captures(content_hash);
CREATE INDEX IF NOT EXISTS idx_raw_received ON raw_captures(received_at);
CREATE INDEX IF NOT EXISTS idx_raw_chat ON raw_captures(chat_id, received_at);
CREATE INDEX IF NOT EXISTS idx_raw_hash ON raw_captures(content_hash);

CREATE TRIGGER IF NOT EXISTS raw_captures_immutable_update
BEFORE UPDATE ON raw_captures
BEGIN
    SELECT RAISE(ABORT, 'raw captures are immutable');
END;

CREATE TRIGGER IF NOT EXISTS raw_captures_guarded_delete
BEFORE DELETE ON raw_captures
WHEN COALESCE((SELECT value FROM system_flags WHERE key = 'allow_raw_delete'), '0') <> '1'
BEGIN
    SELECT RAISE(ABORT, 'raw capture deletion requires explicit user confirmation');
END;

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    confirmed   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id           TEXT PRIMARY KEY,
    slug         TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    type         TEXT NOT NULL,
    status       TEXT NOT NULL,
    project_id   TEXT REFERENCES projects(id) ON DELETE SET NULL,
    language     TEXT NOT NULL DEFAULT 'en',
    summary      TEXT NOT NULL DEFAULT '',
    content_json TEXT NOT NULL DEFAULT '{}',
    body_md      TEXT NOT NULL,
    path         TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    sensitive    INTEGER NOT NULL DEFAULT 0,
    indexed      INTEGER NOT NULL DEFAULT 1,
    provenance   TEXT NOT NULL DEFAULT 'observed',
    superseded_by TEXT REFERENCES notes(id) ON DELETE SET NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    file_mtime   REAL NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_status ON notes(status);
CREATE INDEX IF NOT EXISTS idx_notes_type ON notes(type);
CREATE INDEX IF NOT EXISTS idx_notes_project ON notes(project_id);
CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at);

CREATE TABLE IF NOT EXISTS note_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id     TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    title       TEXT NOT NULL,
    body_md     TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    raw_id      TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE(note_id, version)
);

CREATE TABLE IF NOT EXISTS note_sources (
    note_id  TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    raw_id   TEXT NOT NULL REFERENCES raw_captures(id) ON DELETE CASCADE,
    relation TEXT NOT NULL DEFAULT 'source',
    PRIMARY KEY (note_id, raw_id)
);
CREATE INDEX IF NOT EXISTS idx_note_sources_raw ON note_sources(raw_id);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS note_tags (
    note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (note_id, tag_id)
);

CREATE TABLE IF NOT EXISTS links (
    id            TEXT PRIMARY KEY,
    from_note_id  TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    to_note_id    TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    reason        TEXT NOT NULL DEFAULT '',
    confidence    REAL NOT NULL DEFAULT 1.0,
    status        TEXT NOT NULL DEFAULT 'confirmed',
    origin        TEXT NOT NULL DEFAULT 'alan',
    created_at    TEXT NOT NULL,
    UNIQUE(from_note_id, to_note_id)
);
CREATE INDEX IF NOT EXISTS idx_links_to ON links(to_note_id);

CREATE TABLE IF NOT EXISTS conflicts (
    id             TEXT PRIMARY KEY,
    note_id        TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    other_note_id  TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    existing_claim TEXT NOT NULL DEFAULT '',
    new_claim      TEXT NOT NULL DEFAULT '',
    explanation    TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'unresolved',
    resolution     TEXT NOT NULL DEFAULT '',
    resolved_at    TEXT,
    raw_id         TEXT,
    detected_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conflicts_status ON conflicts(status);

CREATE TABLE IF NOT EXISTS lessons (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    text            TEXT NOT NULL,
    scope           TEXT NOT NULL DEFAULT 'global',
    evidence_note_id TEXT,
    evidence_raw_id TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    times_applied   INTEGER NOT NULL DEFAULT 0,
    confidence      REAL NOT NULL DEFAULT 0.6,
    provenance      TEXT NOT NULL DEFAULT 'observed',
    explanation     TEXT NOT NULL DEFAULT '',
    last_applied_at TEXT,
    success_count   INTEGER NOT NULL DEFAULT 0,
    failure_count   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lessons_active ON lessons(active, kind);

CREATE TABLE IF NOT EXISTS preferences (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'user',
    active     INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id             TEXT NOT NULL,
    role                TEXT NOT NULL,
    text                TEXT NOT NULL,
    language            TEXT NOT NULL DEFAULT 'en',
    telegram_message_id INTEGER,
    project_id          TEXT,
    topic               TEXT NOT NULL DEFAULT '',
    meta_json           TEXT NOT NULL DEFAULT '{}',
    note_id             TEXT,
    raw_id              TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS pending_states (
    chat_id    TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id           TEXT PRIMARY KEY,
    chat_id      TEXT NOT NULL,
    note_id      TEXT,
    kind         TEXT NOT NULL,
    message      TEXT NOT NULL DEFAULT '',
    due_at       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    origin       TEXT NOT NULL DEFAULT 'alan',
    created_at   TEXT NOT NULL,
    last_sent_at TEXT,
    sent_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_pending ON reminders(status, due_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_uniq ON reminders(chat_id, note_id, kind, status);

CREATE TABLE IF NOT EXISTS proactive_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  TEXT NOT NULL,
    sent_at  TEXT NOT NULL,
    kinds    TEXT NOT NULL,
    note_ids TEXT NOT NULL,
    outcome  TEXT NOT NULL DEFAULT 'sent'
);
CREATE INDEX IF NOT EXISTS idx_proactive_chat ON proactive_log(chat_id, sent_at);

CREATE TABLE IF NOT EXISTS inventory_runs (
    id          TEXT PRIMARY KEY,
    chat_id     TEXT NOT NULL,
    source      TEXT NOT NULL,
    item_count  INTEGER NOT NULL,
    summary_md  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_jobs (
    id           TEXT PRIMARY KEY,
    chat_id      TEXT NOT NULL,
    source       TEXT NOT NULL,
    origin       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    total        INTEGER NOT NULL DEFAULT 0,
    captured     INTEGER NOT NULL DEFAULT 0,
    notes_made   INTEGER NOT NULL DEFAULT 0,
    duplicates   INTEGER NOT NULL DEFAULT 0,
    conflicts    INTEGER NOT NULL DEFAULT 0,
    skipped      INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT '',
    options_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_import_jobs_status ON import_jobs(status, created_at);

CREATE TABLE IF NOT EXISTS import_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
    origin_key  TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    received_at TEXT NOT NULL,
    language    TEXT NOT NULL DEFAULT 'en',
    media_json  TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'pending',
    priority    INTEGER NOT NULL DEFAULT 1,
    raw_id      TEXT,
    note_id     TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_import_items_next ON import_items(job_id, status, priority, received_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    target       TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);

CREATE TABLE IF NOT EXISTS embeddings (
    note_id    TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (note_id, model)
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    note_id UNINDEXED,
    title,
    summary,
    tags,
    project,
    body,
    roots,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


class Database:
    """Thin wrapper around a single SQLite connection.

    A single connection is intentional: Alan is a single-user assistant, and
    serialising writes avoids SQLite lock contention between the bot loop and
    the proactive worker.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self._migrate()

    # -- schema ------------------------------------------------------------

    def _migrate(self) -> None:
        self.conn.executescript(SCHEMA)
        current = self.flag("schema_version")
        version = 0 if current is None else int(current)
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema v{version} is newer than this build (v{SCHEMA_VERSION})."
            )
        if version < 2:
            self._migrate_v2_columns()
            self._migrate_fts()
        if current is None or version != SCHEMA_VERSION:
            self.set_flag("schema_version", str(SCHEMA_VERSION))

    # -- migrations --------------------------------------------------------

    def _columns(self, table: str) -> set[str]:
        return {str(row[1]) for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def _add_column(self, table: str, name: str, ddl: str) -> None:
        if name not in self._columns(table):
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _migrate_v2_columns(self) -> None:
        """v1.1: working memory, lesson confidence, Markdown edit detection."""

        self._add_column("notes", "content_hash", "TEXT NOT NULL DEFAULT ''")
        self._add_column("notes", "file_mtime", "REAL NOT NULL DEFAULT 0")
        self._add_column("messages", "language", "TEXT NOT NULL DEFAULT 'en'")
        self._add_column("messages", "telegram_message_id", "INTEGER")
        self._add_column("messages", "project_id", "TEXT")
        self._add_column("messages", "topic", "TEXT NOT NULL DEFAULT ''")
        self._add_column("messages", "meta_json", "TEXT NOT NULL DEFAULT '{}'")
        self._add_column("lessons", "confidence", "REAL NOT NULL DEFAULT 0.6")
        self._add_column("lessons", "explanation", "TEXT NOT NULL DEFAULT ''")
        self._add_column("lessons", "last_applied_at", "TEXT")
        self._add_column("lessons", "success_count", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("lessons", "failure_count", "INTEGER NOT NULL DEFAULT 0")

    def _migrate_fts(self) -> None:
        """Recreate the index with normalized stems and root tokens."""

        if self.flag("fts_version") == "2":
            return
        self.conn.execute("DROP TABLE IF EXISTS notes_fts")
        self.conn.execute(
            """
            CREATE VIRTUAL TABLE notes_fts USING fts5(
                note_id UNINDEXED,
                title,
                summary,
                tags,
                project,
                body,
                roots,
                tokenize = 'unicode61 remove_diacritics 2'
            )
            """
        )
        self.set_flag("fts_version", "2")
        try:
            from .store import search as search_store

            search_store.fts_rebuild(self)
        except Exception:  # pragma: no cover - a failed rebuild must not block startup
            pass

    # -- primitives --------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def executemany(self, sql: str, params: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        return self.conn.executemany(sql, [tuple(p) for p in params])

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- flags -------------------------------------------------------------

    def flag(self, key: str) -> str | None:
        row = self.query_one("SELECT value FROM system_flags WHERE key = ?", (key,))
        return None if row is None else str(row["value"])

    def set_flag(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO system_flags(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def close(self) -> None:
        self.conn.close()
