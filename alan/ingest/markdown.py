"""Markdown / plain-text importer for Obsidian vaults and note folders."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..errors import ImportFailed
from ..textutil import detect_language, to_iso
from .base import ImportItem

SKIP_DIRS = {".obsidian", ".git", ".trash", "node_modules", "__pycache__"}
MAX_FILE_BYTES = 2_000_000


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    header = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")
    try:
        meta = yaml.safe_load(header) or {}
        return (meta if isinstance(meta, dict) else {}), body
    except yaml.YAMLError:
        return {}, body


def _meta_date(meta: dict[str, Any], fallback: datetime) -> str:
    for key in ("received_at", "created", "date", "updated", "modified"):
        value = meta.get(key)
        if value is None:
            continue
        if isinstance(value, (datetime,)):
            return to_iso(value)
        text = str(value).strip()
        if not text:
            continue
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return to_iso(datetime.strptime(text[: len(fmt) + 2], fmt).replace(tzinfo=timezone.utc))
            except ValueError:
                continue
        try:
            return to_iso(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            continue
    return to_iso(fallback)


def read_file(path: str | Path) -> list[ImportItem]:
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ImportFailed(f"Could not read {target}: {exc}") from exc

    meta, body = _split_frontmatter(raw)
    body = body.strip()
    if not body:
        raise ImportFailed(f"{target.name} is empty")

    fallback = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc)
    title = str(meta.get("title") or target.stem)
    text = body if body.startswith("#") else f"# {title}\n\n{body}"

    return [
        ImportItem(
            text=text,
            received_at=_meta_date(meta, fallback),
            language=str(meta.get("language") or "") or detect_language(text),
            author="",
            media=[],
            origin_id=json.dumps({"file": target.name}, ensure_ascii=False),
        )
    ]


def read_folder(folder: str | Path, *, limit: int = 3000) -> list[ImportItem]:
    root = Path(folder)
    if not root.is_dir():
        raise ImportFailed(f"Not a folder: {root}")

    items: list[ImportItem] = []
    for path in sorted(root.rglob("*")):
        if len(items) >= limit:
            break
        if not path.is_file() or path.suffix.lower() not in {".md", ".markdown", ".txt"}:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            items.extend(read_file(path))
        except (ImportFailed, OSError):
            continue
    return items[:limit]
