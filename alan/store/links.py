"""Links between notes.

A link is only created when there is a stated, meaningful relationship.  Low
confidence proposals are stored as ``proposed`` and surfaced for confirmation
rather than silently applied.
"""

from __future__ import annotations

from ..db import Database
from ..models import Link
from ..textutil import now_iso
from . import audit
from .ids import link_id as new_link_id

CONFIRM_THRESHOLD = 0.75


def create(
    db: Database,
    from_note_id: str,
    to_note_id: str,
    *,
    reason: str = "",
    confidence: float = 1.0,
    origin: str = "alan",
) -> Link | None:
    if from_note_id == to_note_id:
        return None

    status = "confirmed" if confidence >= CONFIRM_THRESHOLD else "proposed"
    existing = db.query_one(
        "SELECT * FROM links WHERE from_note_id = ? AND to_note_id = ?",
        (from_note_id, to_note_id),
    )
    if existing is not None:
        if status == "confirmed" and existing["status"] != "confirmed":
            db.execute(
                "UPDATE links SET status = 'confirmed', confidence = ?, reason = ? WHERE id = ?",
                (confidence, reason or existing["reason"], existing["id"]),
            )
            return get(db, existing["id"])
        return _row_to_link(existing)

    lid = new_link_id()
    db.execute(
        "INSERT INTO links(id, from_note_id, to_note_id, reason, confidence, status, origin, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (lid, from_note_id, to_note_id, reason, confidence, status, origin, now_iso()),
    )
    audit.record(
        db,
        "link.create",
        target=lid,
        details={"from": from_note_id, "to": to_note_id, "status": status},
    )
    return get(db, lid)


def _row_to_link(row) -> Link:
    return Link(
        id=row["id"],
        from_note_id=row["from_note_id"],
        to_note_id=row["to_note_id"],
        reason=row["reason"],
        confidence=float(row["confidence"]),
        status=row["status"],
        origin=row["origin"],
        created_at=row["created_at"],
    )


def get(db: Database, lid: str) -> Link | None:
    row = db.query_one("SELECT * FROM links WHERE id = ?", (lid,))
    return _row_to_link(row) if row else None


def outgoing(db: Database, note_id: str, *, status: str | None = None) -> list[Link]:
    sql = "SELECT * FROM links WHERE from_note_id = ?"
    params: list[object] = [note_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    return [_row_to_link(row) for row in db.query(sql, params)]


def incoming(db: Database, note_id: str, *, status: str | None = None) -> list[Link]:
    sql = "SELECT * FROM links WHERE to_note_id = ?"
    params: list[object] = [note_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    return [_row_to_link(row) for row in db.query(sql, params)]


def related_ids(db: Database, note_id: str, *, include_proposed: bool = False) -> list[str]:
    statuses = ("confirmed", "proposed") if include_proposed else ("confirmed",)
    rows = db.query(
        "SELECT to_note_id AS other FROM links WHERE from_note_id = ? AND status IN (?,?) "
        "UNION SELECT from_note_id AS other FROM links WHERE to_note_id = ? AND status IN (?,?)",
        (note_id, *statuses, note_id, *statuses),
    )
    return [row["other"] for row in rows]


def set_status(db: Database, lid: str, status: str, *, actor: str = "user") -> None:
    db.execute("UPDATE links SET status = ? WHERE id = ?", (status, lid))
    audit.record(db, f"link.{status}", actor=actor, target=lid)


def proposed(db: Database, chat_id: str | None = None) -> list[dict[str, object]]:
    rows = db.query(
        """
        SELECT l.*, a.title AS from_title, a.slug AS from_slug,
               b.title AS to_title, b.slug AS to_slug
        FROM links l
        JOIN notes a ON a.id = l.from_note_id
        JOIN notes b ON b.id = l.to_note_id
        WHERE l.status = 'proposed'
        ORDER BY l.created_at DESC
        """
    )
    return [dict(row) for row in rows]


def count(db: Database) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM links WHERE status = 'confirmed'") or 0)
