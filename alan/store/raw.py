"""Raw captures — the immutable safety layer.

A raw capture is written once, to SQLite and to a Markdown file.  There is no
update path in this module by design, and the database rejects UPDATE with a
trigger.  Deletion is possible only for the user, explicitly, and is audited.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import Config
from ..db import Database
from ..errors import ImmutableRecord
from ..models import RawCapture
from ..textutil import detect_language, now_iso, parse_iso, sha256_hex
from . import audit
from .ids import raw_id as new_raw_id


def _relative(path: Path, cfg: Config) -> str:
    try:
        return path.relative_to(cfg.data_dir).as_posix()
    except ValueError:
        return path.as_posix()


def _write_capture_file(cfg: Config, capture: RawCapture) -> Path:
    received = parse_iso(capture.received_at)
    folder = cfg.raw_dir / f"{received:%Y}" / f"{received:%m}" / f"{received:%d}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{received:%H%M%S}_{capture.id}.md"
    if path.exists():  # ids are unique; never overwrite an existing capture
        path = folder / f"{received:%H%M%S}_{capture.id}_{capture.content_hash[:6]}.md"

    front = {
        "id": capture.id,
        "received_at": capture.received_at,
        "source": capture.source,
        "chat_id": capture.chat_id,
        "message_id": capture.message_id,
        "language": capture.language,
        "content_hash": capture.content_hash,
        "reply_to": capture.reply_to_id,
        "media": capture.media,
        "imported": capture.imported,
    }
    header = "---\n" + "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in front.items()
    ) + "\n---\n\n"
    # The original text is appended verbatim: no trimming, no normalisation,
    # no translation.  This is the guarantee that nothing is lost.
    path.write_text(header + capture.text, encoding="utf-8", newline="")
    return path


def create(
    db: Database,
    cfg: Config,
    *,
    chat_id: str | int,
    text: str,
    source: str = "telegram",
    message_id: int | None = None,
    language: str | None = None,
    entities: list[dict[str, Any]] | None = None,
    media: list[dict[str, Any]] | None = None,
    reply_to_id: str | None = None,
    conversation_context: str | None = None,
    received_at: str | None = None,
    imported: bool = False,
) -> RawCapture:
    """Persist an exact copy of what the user sent."""

    if text is None:
        text = ""
    capture = RawCapture(
        id=new_raw_id(),
        chat_id=str(chat_id),
        message_id=message_id,
        received_at=received_at or now_iso(),
        source=source,
        language=language or detect_language(text, cfg.default_language),
        text=text,
        text_format="text",
        entities=list(entities or []),
        media=list(media or []),
        reply_to_id=reply_to_id,
        conversation_context=conversation_context,
        content_hash=sha256_hex(text),
        imported=imported,
        created_at=now_iso(),
    )
    path = _write_capture_file(cfg, capture)
    capture.path = _relative(path, cfg)

    db.execute(
        """
        INSERT INTO raw_captures(
            id, chat_id, message_id, received_at, source, language, text,
            text_format, entities_json, media_json, reply_to_id,
            conversation_context, content_hash, path, imported, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            capture.id,
            capture.chat_id,
            capture.message_id,
            capture.received_at,
            capture.source,
            capture.language,
            capture.text,
            capture.text_format,
            json.dumps(capture.entities, ensure_ascii=False),
            json.dumps(capture.media, ensure_ascii=False),
            capture.reply_to_id,
            capture.conversation_context,
            capture.content_hash,
            capture.path,
            1 if capture.imported else 0,
            capture.created_at,
        ),
    )
    return capture


def get(db: Database, capture_id: str) -> RawCapture | None:
    row = db.query_one("SELECT * FROM raw_captures WHERE id = ?", (capture_id,))
    return RawCapture.from_row(row) if row else None


def list_recent(db: Database, limit: int = 20, chat_id: str | None = None) -> list[RawCapture]:
    if chat_id is None:
        rows = db.query(
            "SELECT * FROM raw_captures ORDER BY received_at DESC LIMIT ?", (limit,)
        )
    else:
        rows = db.query(
            "SELECT * FROM raw_captures WHERE chat_id = ? ORDER BY received_at DESC LIMIT ?",
            (chat_id, limit),
        )
    return [RawCapture.from_row(row) for row in rows]


def for_note(db: Database, note_id: str) -> list[RawCapture]:
    rows = db.query(
        """
        SELECT rc.* FROM raw_captures rc
        JOIN note_sources ns ON ns.raw_id = rc.id
        WHERE ns.note_id = ?
        ORDER BY rc.received_at ASC
        """,
        (note_id,),
    )
    return [RawCapture.from_row(row) for row in rows]


def count(db: Database) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM raw_captures") or 0)


def _assert_deletable() -> None:
    raise ImmutableRecord(
        "Raw captures cannot be updated. They are the immutable record of what was said."
    )


def update(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover - guard rail
    _assert_deletable()


def delete(db: Database, cfg: Config, capture_id: str, *, actor: str = "user") -> bool:
    """Delete a raw capture at the user's explicit request.

    The process is audited before and after, and the database flag that permits
    deletion is armed only for the duration of the statement.
    """

    capture = get(db, capture_id)
    if capture is None:
        return False

    audit.record(
        db,
        "raw_capture.delete.requested",
        actor=actor,
        target=capture_id,
        details={"path": capture.path, "received_at": capture.received_at},
    )
    db.set_flag("allow_raw_delete", "1")
    try:
        db.execute("DELETE FROM raw_captures WHERE id = ?", (capture_id,))
    finally:
        db.set_flag("allow_raw_delete", "0")

    path = cfg.data_dir / capture.path
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass

    audit.record(db, "raw_capture.delete.done", actor=actor, target=capture_id)
    return True


def search_terms(db: Database, needle: str, limit: int = 20) -> list[RawCapture]:
    """Exact-term search over raw captures (the immutable fallback)."""

    rows = db.query(
        "SELECT * FROM raw_captures WHERE text LIKE ? ORDER BY received_at DESC LIMIT ?",
        (f"%{needle}%", limit),
    )
    return [RawCapture.from_row(row) for row in rows]
