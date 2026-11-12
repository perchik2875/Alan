"""Reminders and proactive-message bookkeeping.

Reminders are deliberately coarse: Alan is not a calendar.  Each reminder is
derived from a note and can be dismissed by the user; dismissals feed the
learning loop so that ignored categories are eventually silenced.
"""

from __future__ import annotations

from ..db import Database
from ..models import Reminder
from ..textutil import now_iso, parse_iso, utcnow
from . import audit
from .ids import reminder_id as new_reminder_id


def _row(row) -> Reminder:
    return Reminder(
        id=row["id"],
        chat_id=row["chat_id"],
        note_id=row["note_id"],
        kind=row["kind"],
        message=row["message"],
        due_at=row["due_at"],
        status=row["status"],
        origin=row["origin"],
        created_at=row["created_at"],
        last_sent_at=row["last_sent_at"],
        sent_count=int(row["sent_count"]),
    )


def upsert(
    db: Database,
    *,
    chat_id: str | int,
    kind: str,
    due_at: str,
    note_id: str | None = None,
    message: str = "",
    origin: str = "alan",
) -> Reminder:
    existing = db.query_one(
        "SELECT * FROM reminders WHERE chat_id = ? AND IFNULL(note_id,'') = IFNULL(?,'') "
        "AND kind = ? AND status = 'pending'",
        (str(chat_id), note_id, kind),
    )
    if existing is not None:
        db.execute(
            "UPDATE reminders SET due_at = ?, message = ? WHERE id = ?",
            (due_at, message, existing["id"]),
        )
        return get(db, existing["id"])  # type: ignore[return-value]

    rid = new_reminder_id()
    db.execute(
        "INSERT INTO reminders(id, chat_id, note_id, kind, message, due_at, status, origin, "
        "created_at, last_sent_at, sent_count) VALUES(?,?,?,?,?,?,'pending',?,?,NULL,0)",
        (rid, str(chat_id), note_id, kind, message, due_at, origin, now_iso()),
    )
    audit.record(db, "reminder.create", target=rid, details={"kind": kind, "note_id": note_id})
    return get(db, rid)  # type: ignore[return-value]


def get(db: Database, rid: str) -> Reminder | None:
    row = db.query_one("SELECT * FROM reminders WHERE id = ?", (rid,))
    return _row(row) if row else None


def due(db: Database, *, now: str | None = None) -> list[Reminder]:
    rows = db.query(
        "SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ? ORDER BY due_at",
        (now or now_iso(),),
    )
    return [_row(row) for row in rows]


def for_chat(db: Database, chat_id: str | int, *, status: str = "pending") -> list[Reminder]:
    rows = db.query(
        "SELECT * FROM reminders WHERE chat_id = ? AND status = ? ORDER BY due_at",
        (str(chat_id), status),
    )
    return [_row(row) for row in rows]


def mark_sent(db: Database, rid: str) -> None:
    db.execute(
        "UPDATE reminders SET status = 'sent', last_sent_at = ?, sent_count = sent_count + 1 "
        "WHERE id = ?",
        (now_iso(), rid),
    )


def dismiss(db: Database, rid: str, *, reason: str = "", actor: str = "user") -> bool:
    reminder = get(db, rid)
    if reminder is None:
        return False
    db.execute("UPDATE reminders SET status = 'dismissed' WHERE id = ?", (rid,))
    audit.record(db, "reminder.dismiss", actor=actor, target=rid, details={"reason": reason})
    return True


def snooze(db: Database, rid: str, until: str) -> None:
    db.execute("UPDATE reminders SET due_at = ? WHERE id = ?", (until, rid))


# --------------------------------------------------------------------------
# Proactive log
# --------------------------------------------------------------------------

def log_proactive(db: Database, *, chat_id: str | int, kinds: list[str], note_ids: list[str], outcome: str = "sent") -> None:
    db.execute(
        "INSERT INTO proactive_log(chat_id, sent_at, kinds, note_ids, outcome) VALUES(?,?,?,?,?)",
        (str(chat_id), now_iso(), ",".join(kinds), ",".join(note_ids), outcome),
    )


def last_proactive_at(db: Database, chat_id: str | int) -> str | None:
    row = db.query_one(
        "SELECT sent_at FROM proactive_log WHERE chat_id = ? AND outcome = 'sent' "
        "ORDER BY sent_at DESC LIMIT 1",
        (str(chat_id),),
    )
    return row["sent_at"] if row else None


def count_since(db: Database, chat_id: str | int, since_iso: str) -> int:
    return int(
        db.scalar(
            "SELECT COUNT(*) FROM proactive_log WHERE chat_id = ? AND sent_at >= ? AND outcome = 'sent'",
            (str(chat_id), since_iso),
        )
        or 0
    )


def recently_messaged_note_ids(db: Database, chat_id: str | int, *, days: int = 7) -> set[str]:
    from datetime import timedelta

    cutoff = (utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.query(
        "SELECT note_ids FROM proactive_log WHERE chat_id = ? AND sent_at >= ?",
        (str(chat_id), cutoff),
    )
    ids: set[str] = set()
    for row in rows:
        ids.update(part for part in (row["note_ids"] or "").split(",") if part)
    return ids
