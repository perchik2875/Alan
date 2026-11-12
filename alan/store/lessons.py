"""Lessons: durable, inspectable, reversible behavioural rules.

A lesson is written only when the user explicitly states a preference or
correction that will apply again.  Questions, complaints, and meta-commentary
never create lessons; at most they prompt Alan to ask whether something should
become one.  Every lesson carries its source, a confidence score, and counters,
and can be deactivated.
"""

from __future__ import annotations

from ..db import Database
from ..models import Lesson
from ..textutil import now_iso
from . import audit
from .ids import lesson_id as new_lesson_id

KINDS = (
    "interpretation",   # I misread intent
    "classification",   # idea vs decision vs task
    "link",             # I linked or failed to link
    "conflict",         # I missed or mishandled a contradiction
    "question",         # I asked something unnecessary
    "evidence",         # I answered without support
    "tone",             # too agreeable, too verbose, too soft
    "reminder",         # a reminder was ignored or unwanted
    "preference",       # a durable user preference
)

MIN_CONFIDENCE = 0.5


def _row(row) -> Lesson:
    keys = set(row.keys())
    return Lesson(
        id=row["id"],
        kind=row["kind"],
        text=row["text"],
        scope=row["scope"],
        evidence_note_id=row["evidence_note_id"],
        evidence_raw_id=row["evidence_raw_id"],
        active=bool(row["active"]),
        times_applied=int(row["times_applied"]),
        created_at=row["created_at"],
        confidence=float(row["confidence"]) if "confidence" in keys else 0.6,
        explanation=row["explanation"] if "explanation" in keys else "",
        last_applied_at=row["last_applied_at"] if "last_applied_at" in keys else None,
        success_count=int(row["success_count"]) if "success_count" in keys else 0,
        failure_count=int(row["failure_count"]) if "failure_count" in keys else 0,
    )


def add(
    db: Database,
    *,
    kind: str,
    text: str,
    scope: str = "global",
    evidence_note_id: str | None = None,
    evidence_raw_id: str | None = None,
    confidence: float = 0.6,
    explanation: str = "",
    actor: str = "alan",
    model: str = "",
    confirmed: bool | None = None,
) -> Lesson:
    text = text.strip()
    if not text:
        raise ValueError("lesson text must not be empty")
    if kind not in KINDS:
        kind = "preference"
    confidence = max(0.0, min(1.0, float(confidence)))

    existing = db.query_one(
        "SELECT * FROM lessons WHERE text = ? AND active = 1", (text,)
    )
    if existing:
        return _row(existing)

    lid = new_lesson_id()
    db.execute(
        "INSERT INTO lessons(id, kind, text, scope, evidence_note_id, evidence_raw_id, active, "
        "times_applied, confidence, explanation, last_applied_at, success_count, failure_count, created_at) "
        "VALUES(?,?,?,?,?,?,1,0,?,?,NULL,0,0,?)",
        (lid, kind, text, scope, evidence_note_id, evidence_raw_id, confidence, explanation, now_iso()),
    )
    audit.record(
        db,
        "lesson.add",
        actor=actor,
        target=lid,
        details={
            "kind": kind,
            "text": text,
            "scope": scope,
            "confidence": confidence,
            "raw_id": evidence_raw_id,
            "note_id": evidence_note_id,
            "model": model,
            "confirmed": confirmed,
            "before": None,
            "after": {
                "kind": kind,
                "text": text,
                "scope": scope,
                "confidence": confidence,
                "active": True,
            },
        },
    )
    return get(db, lid)  # type: ignore[return-value]


def get(db: Database, lid: str) -> Lesson | None:
    row = db.query_one("SELECT * FROM lessons WHERE id = ?", (lid,))
    return _row(row) if row else None


def list_active(db: Database, *, limit: int = 50) -> list[Lesson]:
    rows = db.query(
        "SELECT * FROM lessons WHERE active = 1 ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    return [_row(row) for row in rows]


def prompt_block(db: Database, *, limit: int = 12) -> str:
    lessons = list_active(db, limit=limit)
    if not lessons:
        return ""
    lines = [f"- [{lesson.kind}] {lesson.text}" for lesson in lessons]
    return "\n".join(lines)


def mark_applied(db: Database, lesson_ids: list[str]) -> None:
    if not lesson_ids:
        return
    ts = now_iso()
    for lid in lesson_ids:
        db.execute(
            "UPDATE lessons SET times_applied = times_applied + 1, last_applied_at = ? WHERE id = ?",
            (ts, lid),
        )


def record_outcome(db: Database, lid: str, *, success: bool) -> None:
    column = "success_count" if success else "failure_count"
    db.execute(f"UPDATE lessons SET {column} = {column} + 1 WHERE id = ?", (lid,))


def deactivate(db: Database, lid: str, *, actor: str = "user") -> bool:
    row = db.query_one("SELECT id FROM lessons WHERE id = ?", (lid,))
    if row is None:
        return False
    db.execute("UPDATE lessons SET active = 0 WHERE id = ?", (lid,))
    audit.record(db, "lesson.deactivate", actor=actor, target=lid)
    return True


def count_active(db: Database) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM lessons WHERE active = 1") or 0)
