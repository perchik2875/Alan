"""Onboarding: turn imported material into a map of the user's knowledge.

The inventory is a report, not a taxonomy.  It proposes project names and
surfaces conflicts and duplicates, then asks the user where to start.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ..db import Database
from ..models import Project
from ..store import notes as notes_store
from ..store import projects as projects_store
from ..store import raw
from ..store import reminders
from ..textutil import now_iso
from .schemas import InventoryDraft
from ..store.ids import inventory_id as new_inventory_id


def build_inventory_block(items: Sequence[object], *, limit: int = 120) -> str:
    lines: list[str] = []
    for item in items[:limit]:
        text = getattr(item, "text", "")
        when = getattr(item, "received_at", "")[:10]
        author = getattr(item, "author", "") or ""
        prefix = f"[{when}] {author}: " if author else f"[{when}] "
        lines.append(prefix + " ".join(text.split())[:600])
    return "\n".join(lines)


def _bullets(title: str, items: Sequence[str], limit: int = 6) -> list[str]:
    cleaned = [str(i).strip() for i in items if str(i).strip()]
    if not cleaned:
        return []
    lines = [f"**{title}**"]
    lines.extend(f"- {item}" for item in cleaned[:limit])
    return lines


def render_inventory(draft: InventoryDraft, *, language: str = "en") -> str:
    ru = (language or "").lower().startswith(("ru", "uk"))
    out: list[str] = []

    out.append("Инвентаризация завершена, сэр." if ru else "Initial inventory complete, sir.")
    if draft.summary:
        out += ["", draft.summary]

    themes = [f"{t.name} — {t.description}" if t.description else t.name for t in draft.themes]
    out += [""] + _bullets("Повторяющиеся темы" if ru else "Recurring themes", themes) if themes else []

    if draft.active or draft.stale:
        out += [""]
        out += _bullets("Активное" if ru else "Active", draft.active)
        out += _bullets("Застой" if ru else "Stale", draft.stale)

    if draft.duplicates:
        out += [""] + _bullets("Возможные дубликаты" if ru else "Likely duplicates", draft.duplicates)
    if draft.open_questions:
        out += [""] + _bullets("Открытые вопросы" if ru else "Open questions", draft.open_questions)
    if draft.conflicts:
        out += [""] + _bullets("Конфликты" if ru else "Conflicts", draft.conflicts)
    if draft.decisions:
        out += [""] + _bullets("Ключевые решения" if ru else "Key decisions", draft.decisions)
    if draft.missing_context:
        out += [""] + _bullets("Не хватает контекста" if ru else "Missing context", draft.missing_context)

    if draft.suggested_projects:
        out += [""]
        out.append("Возможные проекты, сэр:" if ru else "Possible project names, sir:")
        out.extend(f"{i}. {name}" for i, name in enumerate(draft.suggested_projects[:4], start=1))

    if draft.first_step:
        out += ["", ("С чего начать: " if ru else "Start with: ") + draft.first_step]

    return "\n".join(line for line in out if line is not None).strip()


def save_run(db: Database, *, chat_id: str | int, source: str, item_count: int, summary_md: str) -> str:
    inv_id = new_inventory_id()
    db.execute(
        "INSERT INTO inventory_runs(id, chat_id, source, item_count, summary_md, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (inv_id, str(chat_id), source, item_count, summary_md, now_iso()),
    )
    return inv_id


def latest_run(db: Database, chat_id: str | int) -> dict[str, object] | None:
    row = db.query_one(
        "SELECT * FROM inventory_runs WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1",
        (str(chat_id),),
    )
    return dict(row) if row else None


def apply_suggested_projects(db: Database, names: Sequence[str]) -> list[Project]:
    created = []
    for name in names:
        created.append(projects_store.create(db, name, confirmed=False))
    return created


def seed_reminders(db: Database, chat_id: str | int) -> int:
    """Queue an initial review offer after onboarding."""

    count = notes_store.stats(db)["notes"]
    if count == 0:
        return 0
    from datetime import datetime, timedelta, timezone

    from ..textutil import to_iso

    reminders.upsert(
        db,
        chat_id=chat_id,
        kind="inventory_followup",
        due_at=to_iso(datetime.now(timezone.utc) + timedelta(hours=20)),
        message="Review the imported notes.",
        origin="user",
    )
    return 1
