"""Explicit, reversible user preferences.

Preferences are key/value so that they survive model changes and can be shown
to the user verbatim.  Only the user (or an explicit confirmation) writes here;
Alan may propose a preference but must not silently persist it.
"""

from __future__ import annotations

from ..db import Database
from ..textutil import now_iso
from . import audit

KNOWN_KEYS = {
    "language": "Preferred reply language code, e.g. ru or en.",
    "detail": "Preferred level of detail: short, normal, detailed.",
    "format": "Preferred answer format: bullets, prose, mixed.",
    "address": "How Alan addresses the user. Defaults to 'Sir'.",
    "tone": "Tone adjustments, e.g. 'less dry wit'.",
    "quiet_hours": "Local hours during which proactive messages are not sent.",
    "proactive": "on or off.",
    "timezone": "IANA timezone used for dates and reminders.",
    "reminder_style": "short or detailed.",
    "citation": "always or on-request.",
}


def set_value(db: Database, key: str, value: str, *, source: str = "user", actor: str = "user") -> None:
    key = key.strip().lower()
    value = str(value).strip()
    db.execute(
        "INSERT INTO preferences(key, value, source, active, updated_at) VALUES(?,?,?,1,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, source = excluded.source, "
        "active = 1, updated_at = excluded.updated_at",
        (key, value, source, now_iso()),
    )
    audit.record(db, "preference.set", actor=actor, target=key, details={"value": value})


def get_value(db: Database, key: str, default: str | None = None) -> str | None:
    row = db.query_one("SELECT value FROM preferences WHERE key = ? AND active = 1", (key.lower(),))
    return row["value"] if row else default


def all_active(db: Database) -> dict[str, str]:
    rows = db.query("SELECT key, value FROM preferences WHERE active = 1 ORDER BY key")
    return {row["key"]: row["value"] for row in rows}


def remove(db: Database, key: str, *, actor: str = "user") -> bool:
    row = db.query_one("SELECT key FROM preferences WHERE key = ? AND active = 1", (key.lower(),))
    if row is None:
        return False
    db.execute("UPDATE preferences SET active = 0, updated_at = ? WHERE key = ?", (now_iso(), key.lower()))
    audit.record(db, "preference.remove", actor=actor, target=key.lower())
    return True


def prompt_block(db: Database) -> str:
    values = all_active(db)
    if not values:
        return ""
    return "\n".join(f"- {key}: {value}" for key, value in values.items())
