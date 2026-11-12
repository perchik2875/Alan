"""Telegram chat export importer.

Accepts the JSON produced by Telegram Desktop's "Export chat history"
(``result.json``).  Service messages, joins, and empty media-only events are
skipped; captions and text are preserved verbatim.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..errors import ImportFailed
from ..textutil import detect_language, to_iso
from .base import ImportItem


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(text, list):
                    parts.append(_flatten_text(text))
        return "".join(parts)
    if isinstance(value, dict):
        return _flatten_text(value.get("text"))
    return str(value)


def _parse_date(value: str | None) -> str:
    if not value:
        return to_iso(datetime.now(timezone.utc))
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.fromisoformat(text[:19])
        except ValueError as exc:
            raise ImportFailed(f"Unreadable date in export: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return to_iso(dt)


def _media_of(message: dict[str, Any]) -> list[dict[str, Any]]:
    media = []
    for key in ("photo", "file", "voice_message", "video_file", "audio_file", "sticker"):
        if message.get(key):
            value = message[key]
            entry = {"type": key}
            if isinstance(value, str):
                entry["ref"] = value
            elif isinstance(value, dict):
                entry["ref"] = value.get("file_name") or value.get("thumbnail") or ""
            media.append(entry)
    return media


def iter_messages(payload: dict[str, Any] | list[Any]) -> Iterator[dict[str, Any]]:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
        return

    if isinstance(payload.get("messages"), list):
        for item in payload["messages"]:
            if isinstance(item, dict):
                yield item
        return

    chats = (payload.get("chats") or {}).get("list") if isinstance(payload.get("chats"), dict) else None
    if chats:
        for chat in chats:
            for item in chat.get("messages") or []:
                if isinstance(item, dict):
                    yield item


def read_export(path: str | Path, *, limit: int = 5000) -> list[ImportItem]:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImportFailed(f"Could not read Telegram export {target.name}: {exc}") from exc

    items: list[ImportItem] = []
    seen: set[tuple[str, str]] = set()

    for message in iter_messages(payload):
        if message.get("type") not in (None, "message"):
            continue

        text = _flatten_text(message.get("text"))
        media = _media_of(message)
        if not text.strip() and not media:
            continue
        if not text.strip() and media:
            kinds = ", ".join(sorted({m["type"] for m in media}))
            text = f"[{kinds}]"

        received = _parse_date(message.get("date"))
        key = (received, text)
        if key in seen:
            continue
        seen.add(key)

        items.append(
            ImportItem(
                text=text,
                received_at=received,
                language=detect_language(text),
                author=str(message.get("from") or ""),
                media=media,
                origin_id=str(message.get("id") or ""),
            )
        )
        if len(items) >= limit:
            break

    return items


def read_folder(folder: str | Path, *, limit: int = 5000) -> list[ImportItem]:
    root = Path(folder)
    if not root.is_dir():
        raise ImportFailed(f"Not a folder: {root}")

    items: list[ImportItem] = []
    for path in sorted(root.rglob("*.json")):
        if len(items) >= limit:
            break
        if path.name.lower() not in {"result.json", "messages.json", "export.json"}:
            continue
        try:
            items.extend(read_export(path, limit=limit - len(items)))
        except ImportFailed:
            continue
    return items[:limit]
