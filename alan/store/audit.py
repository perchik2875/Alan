"""Audit trail.

Every action that changes durable state or touches user data is recorded here
so that the user can always answer "who changed this and when".
"""

from __future__ import annotations

import json
from typing import Any

from ..db import Database
from ..textutil import now_iso


def record(
    db: Database,
    action: str,
    *,
    actor: str = "alan",
    target: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    db.execute(
        "INSERT INTO audit_log(at, actor, action, target, details_json) VALUES(?,?,?,?,?)",
        (now_iso(), actor, action, target, json.dumps(details or {}, ensure_ascii=False)),
    )


def recent(db: Database, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT at, actor, action, target, details_json FROM audit_log ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return [
        {
            "at": row["at"],
            "actor": row["actor"],
            "action": row["action"],
            "target": row["target"],
            "details": json.loads(row["details_json"] or "{}"),
        }
        for row in rows
    ]
