"""Resumable import jobs.

An import is deliberately split into two stages:

1. **Capture** — every source item becomes a raw capture immediately, in
   batches.  This is cheap and crash-safe: if the process dies, the user's
   original text is already on disk and in the database.
2. **Analysis** — captured items are turned into notes later, in priority order,
   with progress, pause, resume, and cancel.

The job tables are the only state.  Nothing depends on an in-process queue, so
a restart resumes exactly where the previous run stopped.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from ..db import Database
from ..textutil import now_iso, sha256_hex
from .ids import import_id as new_import_id

JOB_STATUSES = ("pending", "running", "paused", "done", "cancelled", "failed")
ITEM_STATUSES = ("pending", "captured", "analyzing", "done", "skipped", "duplicate", "failed")

_ACTIVE = ("pending", "running", "paused")


def _job(row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["options"] = json.loads(data.pop("options_json", "{}") or "{}")
    except ValueError:
        data["options"] = {}
    return data


def _item(row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["media"] = json.loads(data.pop("media_json", "[]") or "[]")
    except ValueError:
        data["media"] = []
    return data


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

def create(
    db: Database,
    *,
    chat_id: str | int,
    source: str,
    origin: str = "",
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    jid = new_import_id()
    ts = now_iso()
    db.execute(
        "INSERT INTO import_jobs(id, chat_id, source, origin, status, options_json, created_at, updated_at) "
        "VALUES(?,?,?,?,'pending',?,?,?)",
        (jid, str(chat_id), source, origin, json.dumps(options or {}, ensure_ascii=False), ts, ts),
    )
    return get(db, jid)  # type: ignore[return-value]


def get(db: Database, job_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM import_jobs WHERE id = ?", (job_id,))
    return _job(row) if row else None


def list_jobs(
    db: Database, *, chat_id: str | int | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    if chat_id is None:
        rows = db.query("SELECT * FROM import_jobs ORDER BY created_at DESC LIMIT ?", (limit,))
    else:
        rows = db.query(
            "SELECT * FROM import_jobs WHERE chat_id = ? ORDER BY created_at DESC LIMIT ?",
            (str(chat_id), limit),
        )
    return [_job(row) for row in rows]


def set_status(db: Database, job_id: str, status: str, *, error: str = "") -> None:
    if status not in JOB_STATUSES:
        raise ValueError(f"unknown import status: {status}")
    ts = now_iso()
    finished = ts if status in {"done", "cancelled", "failed"} else None
    db.execute(
        "UPDATE import_jobs SET status = ?, error = ?, updated_at = ?, "
        "finished_at = COALESCE(?, finished_at) WHERE id = ?",
        (status, error, ts, finished, job_id),
    )


def pause(db: Database, job_id: str) -> bool:
    job = get(db, job_id)
    if job is None or job["status"] not in {"pending", "running"}:
        return False
    set_status(db, job_id, "paused")
    return True


def resume(db: Database, job_id: str) -> bool:
    job = get(db, job_id)
    if job is None or job["status"] != "paused":
        return False
    set_status(db, job_id, "running")
    return True


def cancel(db: Database, job_id: str) -> bool:
    job = get(db, job_id)
    if job is None or job["status"] in {"done", "cancelled"}:
        return False
    set_status(db, job_id, "cancelled")
    return True


def jobs_to_resume(db: Database) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM import_jobs WHERE status IN ('pending','running','paused') "
        "ORDER BY created_at LIMIT 10"
    )
    return [_job(row) for row in rows]


def fail(db: Database, job_id: str, error: str) -> None:
    set_status(db, job_id, "failed", error=error[:2000])


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------

def _priority(text: str, received_at: str) -> int:
    """Longer, recent material is worth analysing first."""

    score = 1
    if len((text or "").strip()) >= 200:
        score += 1
    if (received_at or "") >= now_iso()[:10]:
        score += 1
    return score


def add_items(
    db: Database,
    job_id: str,
    items: Iterable[Any],
    *,
    seen_hashes: set[str] | None = None,
) -> tuple[int, int]:
    """Insert items as ``pending``/``duplicate``; return (added, duplicates)."""

    seen = seen_hashes if seen_hashes is not None else set()
    added = duplicates = 0
    ts = now_iso()
    for item in items:
        text = str(getattr(item, "text", "") or "")
        if not text.strip():
            continue
        digest = sha256_hex(text)
        origin = str(getattr(item, "origin_id", "") or "")
        key = origin or digest
        existing = db.query_one(
            "SELECT id FROM import_items WHERE job_id = ? AND origin_key = ? AND status != 'duplicate'",
            (job_id, key),
        )
        duplicate = digest in seen
        if existing is None:
            db.execute(
                "INSERT INTO import_items(job_id, origin_key, text, received_at, language, media_json, "
                "status, priority, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    key,
                    text,
                    str(getattr(item, "received_at", "") or ts),
                    str(getattr(item, "language", "") or "en"),
                    json.dumps(getattr(item, "media", []) or [], ensure_ascii=False),
                    "duplicate" if duplicate else "pending",
                    _priority(text, str(getattr(item, "received_at", "") or "")),
                    ts,
                    ts,
                ),
            )
        else:
            duplicate = True
        seen.add(digest)
        if duplicate:
            duplicates += 1
        else:
            added += 1
    _refresh_total(db, job_id)
    refresh_counts(db, job_id)
    return added, duplicates


def next_batch(db: Database, job_id: str, *, limit: int = 40) -> list[dict[str, Any]]:
    """Items ready for analysis: captured first, then still-uncaptured."""

    rows = db.query(
        "SELECT * FROM import_items WHERE job_id = ? AND status IN ('pending','captured') "
        "ORDER BY CASE status WHEN 'captured' THEN 0 ELSE 1 END, priority DESC, received_at DESC, id ASC LIMIT ?",
        (job_id, limit),
    )
    return [_item(row) for row in rows]


def next_pending(db: Database, job_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
    """Items that still need a raw capture (stage 1)."""

    rows = db.query(
        "SELECT * FROM import_items WHERE job_id = ? AND status = 'pending' "
        "ORDER BY priority DESC, received_at DESC, id ASC LIMIT ?",
        (job_id, limit),
    )
    return [_item(row) for row in rows]


def mark(
    db: Database,
    item_id: int,
    status: str,
    *,
    raw_id: str | None = None,
    note_id: str | None = None,
    error: str = "",
) -> None:
    if status not in ITEM_STATUSES:
        raise ValueError(f"unknown import item status: {status}")
    db.execute(
        "UPDATE import_items SET status = ?, raw_id = COALESCE(?, raw_id), "
        "note_id = COALESCE(?, note_id), error = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
        (status, raw_id, note_id, error[:2000], now_iso(), item_id),
    )


def _refresh_total(db: Database, job_id: str) -> None:
    total = db.scalar("SELECT COUNT(*) FROM import_items WHERE job_id = ?", (job_id,)) or 0
    db.execute(
        "UPDATE import_jobs SET total = ?, updated_at = ? WHERE id = ?",
        (int(total), now_iso(), job_id),
    )


def refresh_counts(db: Database, job_id: str) -> None:
    counts = {
        str(row["status"]): int(row["n"])
        for row in db.query(
            "SELECT status, COUNT(*) AS n FROM import_items WHERE job_id = ? GROUP BY status",
            (job_id,),
        )
    }
    total = sum(counts.values())
    captured = counts.get("captured", 0) + counts.get("done", 0) + counts.get("skipped", 0) + counts.get("failed", 0)
    db.execute(
        "UPDATE import_jobs SET total = ?, captured = ?, notes_made = ?, duplicates = ?, "
        "skipped = ?, failed = ?, updated_at = ? WHERE id = ?",
        (
            total,
            captured,
            counts.get("done", 0),
            counts.get("duplicate", 0),
            counts.get("skipped", 0),
            counts.get("failed", 0),
            now_iso(),
            job_id,
        ),
    )


def items(db: Database, job_id: str, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    if status:
        rows = db.query(
            "SELECT * FROM import_items WHERE job_id = ? AND status = ? ORDER BY id LIMIT ?",
            (job_id, status, limit),
        )
    else:
        rows = db.query("SELECT * FROM import_items WHERE job_id = ? ORDER BY id LIMIT ?", (job_id, limit))
    return [_item(row) for row in rows]


def report(db: Database, job_id: str) -> str:
    job = get(db, job_id)
    if job is None:
        return "No such import, sir."
    done = job["captured"] or 0
    total = job["total"] or 0
    lines = [
        f"Import {job['id']} · {job['status']}",
        f"- items: {done} of {total}",
        f"- raw captures: {job['captured']}",
        f"- notes drafted: {job['notes_made']}",
        f"- possible duplicates: {job['duplicates']}",
        f"- conflicts: {job['conflicts']}",
        f"- skipped: {job['skipped']} · failed: {job['failed']}",
    ]
    if job.get("error"):
        lines.append(f"- error: {job['error']}")
    return "\n".join(lines)
