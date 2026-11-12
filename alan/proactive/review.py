"""Candidate generation for proactive messages.

Deterministic SQL decides what *might* be worth raising.  The model only
decides whether it is worth raising *now*, and how to say it.  This keeps
proactive behaviour explainable and cheap.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..config import Config, ProactiveConfig
from ..db import Database
from ..store import prefs, reminders
from ..textutil import hours_since, now_iso, parse_iso, to_iso


@dataclass(slots=True)
class Candidate:
    kind: str
    note_id: str
    title: str
    detail: str
    age_days: float = 0.0
    reminder_id: str | None = None


def _local_now(cfg: Config) -> datetime:
    try:
        return datetime.now(ZoneInfo(cfg.timezone))
    except Exception:
        return datetime.now(timezone.utc)


def _quiet(cfg: Config, moment: datetime, pc: ProactiveConfig) -> bool:
    hour = moment.hour
    if pc.quiet_start == pc.quiet_end:
        return False
    if pc.quiet_start < pc.quiet_end:
        return pc.quiet_start <= hour < pc.quiet_end
    return hour >= pc.quiet_start or hour < pc.quiet_end


def within_send_window(db: Database, cfg: Config, chat_id: str | int) -> bool:
    pc = cfg.proactive
    if not pc.enabled:
        return False

    custom = prefs.get_value(db, "quiet_hours")
    if custom and "-" in custom:
        try:
            start, end = (int(part.strip()) for part in custom.split("-", 1))
            pc = replace(pc, quiet_start=start, quiet_end=end)
        except ValueError:
            pass

    moment = _local_now(cfg)
    if _quiet(cfg, moment, pc):
        return False

    last = reminders.last_proactive_at(db, chat_id)
    if last and hours_since(last) < pc.min_hours_between:
        return False

    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    since = to_iso(midnight.astimezone(timezone.utc))
    if reminders.count_since(db, chat_id, since) >= pc.max_messages_per_day:
        return False
    return True


def collect_candidates(db: Database, cfg: Config, chat_id: str | int) -> list[Candidate]:
    pc = cfg.proactive
    candidates: list[Candidate] = []

    # Unresolved conflicts (private material is never surfaced proactively).
    rows = db.query(
        "SELECT c.id, c.note_id, n.title, c.explanation, c.detected_at "
        "FROM conflicts c "
        "JOIN notes n ON n.id = c.note_id "
        "JOIN notes o ON o.id = c.other_note_id "
        "WHERE c.status = 'unresolved' AND n.sensitive = 0 AND o.sensitive = 0 "
        "ORDER BY c.detected_at LIMIT 20"
    )
    for row in rows:
        age = hours_since(row["detected_at"]) / 24
        if age >= pc.conflict_days:
            candidates.append(
                Candidate(
                    kind="conflict",
                    note_id=row["note_id"],
                    title=row["title"],
                    detail=f"Unresolved conflict for {age:.0f} days: {row['explanation']}",
                    age_days=age,
                )
            )

    # Open questions that never got an answer.
    rows = db.query(
        "SELECT id, title, updated_at FROM notes "
        "WHERE type = 'question' AND status = 'unresolved' AND sensitive = 0 "
        "ORDER BY updated_at LIMIT 20"
    )
    for row in rows:
        age = hours_since(row["updated_at"]) / 24
        if age >= pc.open_question_days:
            candidates.append(
                Candidate(
                    kind="open_question",
                    note_id=row["id"],
                    title=row["title"],
                    detail=f"Open question untouched for {age:.0f} days.",
                    age_days=age,
                )
            )

    # Stale projects that still hold active notes.
    rows = db.query(
        """
        SELECT p.id, p.name, COUNT(n.id) AS n, MAX(n.updated_at) AS last
        FROM projects p
        JOIN notes n ON n.project_id = p.id AND n.status IN ('active','paused','unresolved')
                     AND n.sensitive = 0
        WHERE p.status = 'active' AND p.confirmed = 1
        GROUP BY p.id
        HAVING n >= 2
        ORDER BY last ASC
        """
    )
    for row in rows:
        age = hours_since(row["last"]) / 24
        if age >= pc.stale_project_days:
            first = db.query_one(
                "SELECT id, title FROM notes WHERE project_id = ? AND sensitive = 0 "
                "AND status IN ('active','paused','unresolved') "
                "ORDER BY updated_at ASC LIMIT 1",
                (row["id"],),
            )
            if first is None:
                continue
            candidates.append(
                Candidate(
                    kind="stale_project",
                    note_id=first["id"],
                    title=row["name"],
                    detail=f"Project '{row['name']}' has not been updated for {age:.0f} days and still has {row['n']} open notes.",
                    age_days=age,
                )
            )

    # Deadlines approaching or passed.
    horizon = datetime.now(timezone.utc) + timedelta(hours=pc.due_soon_hours)
    rows = db.query(
        "SELECT id, title, updated_at, content_json FROM notes "
        "WHERE status = 'active' AND sensitive = 0 AND content_json LIKE '%deadlines%' LIMIT 200"
    )
    import json

    for row in rows:
        try:
            content = json.loads(row["content_json"] or "{}")
        except ValueError:
            continue
        for deadline in content.get("deadlines") or []:
            try:
                due = parse_iso(str(deadline))
            except (ValueError, TypeError):
                continue
            if due <= horizon:
                candidates.append(
                    Candidate(
                        kind="deadline",
                        note_id=row["id"],
                        title=row["title"],
                        detail=f"Deadline {due.date().isoformat()} is within {pc.due_soon_hours} hours or already passed.",
                        age_days=(datetime.now(timezone.utc) - due).total_seconds() / 86400,
                    )
                )
                break

    # Explicit or queued reminders.
    for reminder in reminders.due(db, now=now_iso()):
        if reminder.chat_id != str(chat_id):
            continue
        candidates.append(
            Candidate(
                kind=reminder.kind,
                note_id=reminder.note_id or "",
                title=reminder.message or "Reminder",
                detail=reminder.message or "A reminder is due.",
                age_days=hours_since(reminder.due_at) / 24,
                reminder_id=reminder.id,
            )
        )

    # Drop anything recently surfaced, and anything too new to be forgotten.
    recent_ids = reminders.recently_messaged_note_ids(db, chat_id, days=7)
    filtered = [
        c for c in candidates
        if c.note_id not in recent_ids and c.age_days >= pc.min_candidate_age_days - 0.0001
    ]
    filtered.sort(key=lambda c: (c.age_days, c.kind), reverse=True)
    return filtered[:8]


def render_candidates(candidates: list[Candidate]) -> str:
    if not candidates:
        return "(none)"
    lines = []
    for c in candidates:
        ids = f" | reminder {c.reminder_id}" if c.reminder_id else ""
        lines.append(f"- [{c.kind}] note {c.note_id or '-'} | {c.title}{ids}\n  {c.detail}")
    return "\n".join(lines)


def render_recent(db: Database, chat_id: str | int) -> str:
    rows = db.query(
        "SELECT sent_at, kinds, note_ids FROM proactive_log WHERE chat_id = ? "
        "ORDER BY sent_at DESC LIMIT 5",
        (str(chat_id),),
    )
    return "\n".join(
        f"- {row['sent_at']}: {row['kinds']} ({row['note_ids']})" for row in rows
    )


def log_skipped(db: Database, chat_id: str | int, candidates: list[Candidate]) -> None:
    if not candidates:
        return
    reminders.log_proactive(
        db,
        chat_id=chat_id,
        kinds=[c.kind for c in candidates],
        note_ids=[c.note_id for c in candidates if c.note_id],
        outcome="skipped",
    )
