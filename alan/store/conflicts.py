"""Conflicts between records.

A conflict is never resolved automatically.  Both records are preserved, the
conflict stays visible in search results, and only the user can declare which
version reflects their current position.
"""

from __future__ import annotations

from ..db import Database
from ..models import Conflict
from ..textutil import now_iso
from . import audit
from .ids import conflict_id as new_conflict_id


def _row(row) -> Conflict:
    return Conflict(
        id=row["id"],
        note_id=row["note_id"],
        other_note_id=row["other_note_id"],
        existing_claim=row["existing_claim"],
        new_claim=row["new_claim"],
        explanation=row["explanation"],
        status=row["status"],
        resolution=row["resolution"],
        resolved_at=row["resolved_at"],
        raw_id=row["raw_id"],
        detected_at=row["detected_at"],
    )


def create(
    db: Database,
    *,
    note_id: str,
    other_note_id: str,
    existing_claim: str,
    new_claim: str,
    explanation: str,
    raw_id: str | None = None,
) -> Conflict | None:
    if note_id == other_note_id:
        return None
    existing = db.query_one(
        "SELECT * FROM conflicts WHERE note_id = ? AND other_note_id = ? AND status = 'unresolved'",
        (note_id, other_note_id),
    )
    if existing is not None:
        return _row(existing)

    cid = new_conflict_id()
    db.execute(
        """
        INSERT INTO conflicts(id, note_id, other_note_id, existing_claim, new_claim,
                              explanation, status, resolution, resolved_at, raw_id, detected_at)
        VALUES(?,?,?,?,?,?,'unresolved','',NULL,?,?)
        """,
        (cid, note_id, other_note_id, existing_claim, new_claim, explanation, raw_id, now_iso()),
    )
    audit.record(
        db,
        "conflict.detect",
        target=cid,
        details={"note": note_id, "other": other_note_id},
    )
    return get(db, cid)


def get(db: Database, cid: str) -> Conflict | None:
    row = db.query_one("SELECT * FROM conflicts WHERE id = ?", (cid,))
    return _row(row) if row else None


def for_note(db: Database, note_id: str, *, unresolved_only: bool = True) -> list[Conflict]:
    sql = "SELECT * FROM conflicts WHERE (note_id = ? OR other_note_id = ?)"
    params: list[object] = [note_id, note_id]
    if unresolved_only:
        sql += " AND status = 'unresolved'"
    return [_row(row) for row in db.query(sql, params)]


def unresolved(db: Database) -> list[dict[str, object]]:
    rows = db.query(
        """
        SELECT c.*, n.title AS note_title, n.slug AS note_slug,
               o.title AS other_title, o.slug AS other_slug
        FROM conflicts c
        JOIN notes n ON n.id = c.note_id
        JOIN notes o ON o.id = c.other_note_id
        WHERE c.status = 'unresolved'
        ORDER BY c.detected_at DESC
        """
    )
    return [dict(row) for row in rows]


def resolve(
    db: Database,
    cid: str,
    *,
    resolution: str,
    winner: str | None = None,
    actor: str = "user",
) -> Conflict | None:
    """Resolve a conflict.

    ``winner`` may be ``"new"``, ``"old"``, ``"both"`` or ``None`` (keep both
    records active, conflict closed by an explicit user decision).
    """

    conflict = get(db, cid)
    if conflict is None:
        return None

    note_id, other_id = conflict.note_id, conflict.other_note_id
    ts = now_iso()

    if winner == "new":
        db.execute(
            "UPDATE notes SET status = 'superseded', superseded_by = ?, updated_at = ? WHERE id = ?",
            (note_id, ts, other_id),
        )
    elif winner == "old":
        db.execute(
            "UPDATE notes SET status = 'superseded', superseded_by = ?, updated_at = ? WHERE id = ?",
            (other_id, ts, note_id),
        )

    db.execute(
        "UPDATE conflicts SET status = 'resolved', resolution = ?, resolved_at = ? WHERE id = ?",
        (resolution, ts, cid),
    )
    audit.record(
        db,
        "conflict.resolve",
        actor=actor,
        target=cid,
        details={"resolution": resolution, "winner": winner},
    )
    return get(db, cid)


def count_unresolved(db: Database) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM conflicts WHERE status = 'unresolved'") or 0)
