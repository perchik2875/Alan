"""Working memory: the active conversation, persisted per chat.

Working memory is not the knowledge base.  Durable facts live in notes; this
table holds what was said, in order, so Alan can answer "what did I just say?"
after a restart.  It can be pruned without losing knowledge, but it is never
kept only in a process-local dictionary.
"""

from __future__ import annotations

import json
from typing import Any

from ..db import Database
from ..textutil import now_iso


def add_message(
    db: Database,
    *,
    chat_id: str | int,
    role: str,
    text: str,
    language: str = "en",
    telegram_message_id: int | None = None,
    project_id: str | None = None,
    topic: str = "",
    meta: dict[str, Any] | None = None,
    note_id: str | None = None,
    raw_id: str | None = None,
) -> int:
    cursor = db.execute(
        "INSERT INTO messages(chat_id, role, text, language, telegram_message_id, project_id, "
        "topic, meta_json, note_id, raw_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(chat_id),
            role,
            text or "",
            language or "en",
            telegram_message_id,
            project_id,
            topic or "",
            json.dumps(meta or {}, ensure_ascii=False),
            note_id,
            raw_id,
            now_iso(),
        ),
    )
    return int(cursor.lastrowid or 0)


def history(db: Database, chat_id: str | int, limit: int = 30) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT role, text, language, note_id, raw_id, created_at FROM messages "
        "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (str(chat_id), limit),
    )
    return [
        {
            "role": row["role"],
            "text": row["text"],
            "language": row["language"] or "en",
            "note_id": row["note_id"],
            "raw_id": row["raw_id"],
            "created_at": row["created_at"],
        }
        for row in reversed(rows)
    ]


def recent(
    db: Database,
    chat_id: str | int,
    *,
    limit: int = 10,
    role: str | None = None,
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    """Most recent turns, newest first by default order, role-filtered."""

    sql = "SELECT id, role, text, language, note_id, raw_id, created_at FROM messages WHERE chat_id = ?"
    params: list[Any] = [str(chat_id)]
    if role:
        sql += " AND role = ?"
        params.append(role)
    if before_id is not None:
        sql += " AND id < ?"
        params.append(before_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = db.query(sql, params)
    return [
        {
            "id": row["id"],
            "role": row["role"],
            "text": row["text"],
            "language": row["language"] or "en",
            "note_id": row["note_id"],
            "raw_id": row["raw_id"],
            "created_at": row["created_at"],
        }
        for row in reversed(rows)
    ]


def count(db: Database, chat_id: str | int, role: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM messages WHERE chat_id = ?"
    params: list[Any] = [str(chat_id)]
    if role:
        sql += " AND role = ?"
        params.append(role)
    return int(db.scalar(sql, params) or 0)


def last_user_language(db: Database, chat_id: str | int, default: str = "") -> str:
    """The language of the previous user turn, used when detection is unsure.

    A one-word reply such as "Да." has too little text for the detector; it must
    not switch the conversation to English.
    """

    row = db.query_one(
        "SELECT language FROM messages WHERE chat_id = ? AND role = 'user' "
        "ORDER BY id DESC LIMIT 1",
        (str(chat_id),),
    )
    return (row["language"] if row else "") or default


def clear(db: Database, chat_id: str | int) -> None:
    db.execute("DELETE FROM messages WHERE chat_id = ?", (str(chat_id),))


def prepend_to_last_assistant(db: Database, chat_id: str | int, prefix: str) -> None:
    """Attach a notice to the reply that was just persisted for this turn."""

    row = db.query_one(
        "SELECT id, text FROM messages WHERE chat_id = ? AND role = 'assistant' "
        "ORDER BY id DESC LIMIT 1",
        (str(chat_id),),
    )
    if row is None:
        return
    db.execute(
        "UPDATE messages SET text = ? WHERE id = ?",
        (f"{prefix}\n\n{row['text']}".strip(), row["id"]),
    )


# --------------------------------------------------------------------------
# Pending state machine
# --------------------------------------------------------------------------

def set_pending(
    db: Database,
    *,
    chat_id: str | int,
    kind: str,
    payload: str | dict[str, Any],
    expires_at: str,
) -> None:
    if isinstance(payload, dict):
        payload = json.dumps(payload, ensure_ascii=False)
    db.execute(
        "INSERT INTO pending_states(chat_id, kind, payload, created_at, expires_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET kind = excluded.kind, payload = excluded.payload, "
        "created_at = excluded.created_at, expires_at = excluded.expires_at",
        (str(chat_id), kind, payload, now_iso(), expires_at),
    )


def get_pending(db: Database, chat_id: str | int) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM pending_states WHERE chat_id = ?", (str(chat_id),))
    if row is None:
        return None
    if row["expires_at"] < now_iso():
        clear_pending(db, chat_id)
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    return {
        "kind": row["kind"],
        "payload": payload,
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


def clear_pending(db: Database, chat_id: str | int) -> None:
    db.execute("DELETE FROM pending_states WHERE chat_id = ?", (str(chat_id),))
