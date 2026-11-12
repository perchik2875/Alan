"""Curated notes: Markdown is the authoritative content, SQLite the index.

A note is stored three ways, each with a distinct job:

* ``data/notes/<slug>.md`` — authoritative, human-readable content.  It may be
  edited by hand; Alan detects the change, versions it, and never overwrites it.
* ``notes.content_json`` — metadata and a structured mirror of the Markdown,
  used for retrieval and for regenerating a file only when the user asks.
* ``notes_fts`` / ``embeddings`` — derived indexes.

The Markdown file is written by Alan when a note changes through conversation.
An external edit becomes a new version instead of being regenerated away.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..config import Config
from ..db import Database
from ..models import Note, NoteContent, RawCapture
from ..textutil import human_datetime, now_iso, one_line, parse_iso, slugify
from . import audit, conflicts, links, raw, tags
from .ids import dedupe_suffix, note_id as new_note_id

NOTE_TYPES = (
    "idea", "decision", "task", "agreement", "fact",
    "question", "plan", "observation", "reference",
)
NOTE_STATUSES = (
    "draft", "needs-clarification", "active", "paused", "resolved",
    "superseded", "archived", "conflict",
    # Legacy values remain readable for records created before v1.2.
    "completed", "rejected", "unresolved",
)

INBOX = "00_Inbox"
ARCHIVE = "08_Archive"


def note_folder(content: NoteContent, *, project_name: str | None = None) -> str:
    """Where a curated note belongs in the user-facing vault.

    The rule is deliberately small: unclear material stays in the Inbox, clear
    durable knowledge is promoted, decisions and questions have their own
    folders, and project material lives under the project.  A new folder is
    never created per message.
    """

    status = content.status or "active"
    if status in {"draft", "needs-clarification", "conflict"}:
        return INBOX
    if status in {"archived", "superseded", "rejected", "completed"}:
        return ARCHIVE
    if content.type == "decision":
        return "03_Decisions"
    if content.type == "question":
        return "04_Questions"
    if content.type == "reference":
        return "06_References"
    if project_name:
        return f"02_Projects/{slugify(project_name, fallback='project')}"
    return "01_Notes"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _list_block(items: Iterable[Any]) -> str:
    lines = []
    for item in items:
        text = one_line(str(item))
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines)


def _section(title: str, body: str) -> str:
    body = (body or "").strip()
    if not body:
        return ""
    return f"## {title}\n\n{body}\n"


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())


def render_markdown(
    *,
    nid: str,
    content: NoteContent,
    project_name: str | None,
    tag_names: list[str],
    related: list[dict[str, str]],
    sources: list[RawCapture],
    version: int,
    created_at: str,
    updated_at: str,
    path_hint: str,
) -> str:
    front = {
        "id": nid,
        "title": content.title,
        "type": content.type,
        "status": content.status,
        "created": created_at[:10],
        "updated": updated_at[:10],
        "language": content.language,
        "project": project_name,
        "tags": tag_names,
        "sensitive": bool(content.sensitive),
        "source": [s.path for s in sources] or None,
        "version": version,
    }
    front_clean = {k: v for k, v in front.items() if v not in (None, [], "")}
    header = yaml.safe_dump(front_clean, allow_unicode=True, sort_keys=False, default_flow_style=None)

    parts: list[str] = [f"---\n{header}---\n", f"# {content.title}\n"]

    parts.append(_section("Summary", content.summary))
    parts.append(_section("Decision", content.decision))
    parts.append(_section("Reasoning", content.reasoning))
    parts.append(_section("Context", content.context))
    parts.append(_section("Constraints", _list_block(content.constraints)))
    parts.append(_section("Assumptions", _list_block(content.assumptions)))
    parts.append(_section("Alternatives considered", _list_block(content.alternatives)))
    parts.append(_section("Open questions", _list_block(content.open_questions)))
    parts.append(_section("Next actions", _list_block(content.next_actions)))
    parts.append(_section("Deadlines", _list_block(content.deadlines)))

    if related:
        rows = []
        for item in related:
            suffix = f" — {item['reason']}" if item.get("reason") else ""
            marker = " *(proposed)*" if item.get("status") == "proposed" else ""
            rows.append(f"- [[{item['slug']}]] — {item['title']}{suffix}{marker}")
        parts.append(_section("Links", "\n".join(rows)))

    if sources:
        src_lines = [
            f"- Raw capture: `{s.path}` — {human_datetime(s.received_at, content.language)}"
            for s in sources
        ]
        total = sum(len(s.text) for s in sources)
        if total <= 6000:
            src_lines.append("")
            for capture in sources:
                src_lines.append(
                    "<details>\n<summary>Original message — "
                    f"{human_datetime(capture.received_at, content.language)}</summary>\n\n"
                    f"{_quote(capture.text)}\n\n</details>"
                )
                src_lines.append("")
        parts.append(_section("Source", "\n".join(src_lines).strip()))

    parts.append(f"<!-- alan:note {nid} v{version} {path_hint} -->\n")
    body = "\n".join(p for p in parts if p)
    body = body.replace("\n\n\n", "\n\n")
    return body.rstrip() + "\n"


# --------------------------------------------------------------------------
# Markdown parsing and external edits
# --------------------------------------------------------------------------

_SECTION_KEYS = {
    "summary": "summary",
    "decision": "decision",
    "reasoning": "reasoning",
    "context": "context",
    "constraints": "constraints",
    "assumptions": "assumptions",
    "alternatives considered": "alternatives",
    "alternatives": "alternatives",
    "open questions": "open_questions",
    "next actions": "next_actions",
    "deadlines": "deadlines",
}
_LIST_KEYS = {
    "constraints", "assumptions", "alternatives",
    "open_questions", "next_actions", "deadlines",
}


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_markdown(text: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Split a note file into (frontmatter, section fields, body)."""

    meta: dict[str, Any] = {}
    body = text or ""
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            header = body[3:end].strip()
            body = body[end + 4 :].lstrip("\n")
            try:
                loaded = yaml.safe_load(header) or {}
                if isinstance(loaded, dict):
                    meta = loaded
            except yaml.YAMLError:
                meta = {}

    title_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    fields: dict[str, Any] = {}
    if title_match:
        fields["title"] = title_match.group(1).strip()

    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is None:
            return
        key = _SECTION_KEYS.get(current)
        if not key:
            return
        if key in _LIST_KEYS:
            fields[key] = [
                line.strip()[2:].strip()
                for line in buffer
                if line.strip().startswith(("- ", "* ")) and line.strip()[2:].strip()
            ]
        else:
            fields[key] = "\n".join(buffer).strip()

    for line in body.splitlines():
        if line.startswith("## "):
            flush()
            current = line[3:].strip().lower()
            buffer = []
        elif current is not None:
            buffer.append(line)
    flush()
    return meta, fields, body


def content_from_markdown(note: Note, text: str) -> tuple[NoteContent, dict[str, Any]]:
    """Read a user-edited file into structured content.

    Markdown is authoritative for content: a section that was removed is
    cleared.  Metadata that is not expressed in Markdown (links, sources,
    project) survives from the sidecar.
    """

    meta, fields, _ = parse_markdown(text)
    content = _content_from(note)

    if fields.get("title"):
        content.title = str(fields["title"])
    for key in ("summary", "decision", "reasoning", "context"):
        content.__setattr__(key, str(fields.get(key, "")))
    for key in _LIST_KEYS:
        content.__setattr__(key, list(fields.get(key, [])))

    for key in ("type", "status", "language"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            setattr(content, key, value.strip())
    if "sensitive" in meta:
        content.sensitive = bool(meta.get("sensitive"))
    raw_tags = meta.get("tags")
    if isinstance(raw_tags, str):
        content.tags = [part.strip() for part in raw_tags.split(",") if part.strip()]
    elif isinstance(raw_tags, (list, tuple)):
        content.tags = [str(part).strip() for part in raw_tags if str(part).strip()]

    content.title = one_line(content.title) or note.title
    content.type = content.type if content.type in NOTE_TYPES else note.type
    content.status = content.status if content.status in NOTE_STATUSES else note.status
    return content, meta


def _apply_external_edit(
    db: Database,
    cfg: Config,
    note: Note,
    text: str,
    mtime: float,
) -> Note:
    content, meta = content_from_markdown(note, text)

    project_id = note.project_id
    project_name = meta.get("project")
    if isinstance(project_name, str) and project_name.strip():
        row = db.query_one(
            "SELECT id FROM projects WHERE name = ? COLLATE NOCASE", (project_name.strip(),)
        )
        if row:
            project_id = row["id"]

    new_version = note.version + 1
    ts = now_iso()
    db.execute(
        """
        UPDATE notes SET title = ?, type = ?, status = ?, project_id = ?, language = ?,
               summary = ?, content_json = ?, body_md = ?, path = ?, version = ?,
               sensitive = ?, content_hash = ?, file_mtime = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            content.title, content.type, content.status, project_id, content.language,
            content.summary, json.dumps(asdict(content), ensure_ascii=False), text,
            note.path, new_version, 1 if content.sensitive else 0,
            _hash_text(text), mtime, ts, note.id,
        ),
    )
    tags.set_for_note(db, note.id, content.tags)
    db.execute(
        "INSERT INTO note_versions(note_id, version, title, body_md, summary, reason, raw_id, created_at) "
        "VALUES(?,?,?,?,?,?,NULL,?)",
        (note.id, new_version, content.title, text, content.summary, "External edit detected on disk.", ts),
    )
    audit.record(
        db,
        "note.external_edit",
        actor="user",
        target=note.id,
        details={"from_version": note.version, "to_version": new_version},
    )
    from . import search

    updated = get(db, note.id)
    if updated is not None:
        project_row = (
            db.query_one("SELECT name FROM projects WHERE id = ?", (updated.project_id,))
            if updated.project_id
            else None
        )
        target = note_folder(content, project_name=project_row["name"] if project_row else None)
        current = Path(updated.path).parent.as_posix()
        if target != current:
            destination = cfg.vault_dir / target / f"{updated.slug}.md"
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                cfg.note_path(updated.path).replace(destination)
                db.execute(
                    "UPDATE notes SET path = ? WHERE id = ?",
                    (destination.relative_to(cfg.vault_dir).as_posix(), updated.id),
                )
                updated = get(db, note.id) or updated
            except OSError:  # pragma: no cover - keep the note readable where it is
                pass
        search.fts_upsert(db, updated)
        return updated
    return note


def detect_external_edit(db: Database, cfg: Config, note: Note) -> bool:
    """Compare the file on disk with the last version Alan wrote."""

    path = cfg.note_path(note.path)
    try:
        stat = path.stat()
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if note.file_mtime and stat.st_mtime <= note.file_mtime + 1e-6:
        return False
    if _hash_text(text) == (note.content_hash or ""):
        db.execute("UPDATE notes SET file_mtime = ? WHERE id = ?", (stat.st_mtime, note.id))
        note.file_mtime = stat.st_mtime
        return False
    _apply_external_edit(db, cfg, note, text, stat.st_mtime)
    return True


def sync_external_edits(
    db: Database,
    cfg: Config,
    *,
    limit: int = 5000,
    note_ids: Iterable[str] | None = None,
) -> list[str]:
    """Version every note whose Markdown changed outside Alan."""

    changed: list[str] = []
    if note_ids is not None:
        candidates = [n for n in (get(db, nid) for nid in note_ids) if n is not None]
    else:
        candidates = list_notes(db, limit=limit, include_sensitive=True)
    for note in candidates:
        if detect_external_edit(db, cfg, note):
            changed.append(note.id)
    return changed


def refresh_metadata(db: Database, cfg: Config, note_id: str) -> Note:
    """Regenerate links and source blocks; used only after the user agrees."""

    return rerender(db, cfg, note_id)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def _related_for(db: Database, note_id: str) -> list[dict[str, str]]:
    rows = db.query(
        """
        SELECT b.slug, b.title, l.reason, l.status
        FROM links l JOIN notes b ON b.id = l.to_note_id
        WHERE l.from_note_id = ? AND l.status IN ('confirmed','proposed')
        UNION
        SELECT a.slug, a.title, l.reason, l.status
        FROM links l JOIN notes a ON a.id = l.from_note_id
        WHERE l.to_note_id = ? AND l.status IN ('confirmed','proposed')
        ORDER BY title COLLATE NOCASE
        """,
        (note_id, note_id),
    )
    return [
        {"slug": row["slug"], "title": row["title"], "reason": row["reason"] or "", "status": row["status"]}
        for row in rows
    ]


def _write_file(cfg: Config, note: Note, folder: str) -> str:
    path = cfg.vault_dir / folder / f"{note.slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(note.body_md, encoding="utf-8", newline="")
    tmp.replace(path)
    return path.relative_to(cfg.vault_dir).as_posix()


def _remove_old_file(cfg: Config, relative: str | None, keep: str) -> None:
    if not relative or relative == keep:
        return
    try:
        old = cfg.note_path(relative)
        if old.exists() and old.is_file():
            old.unlink()
    except OSError:  # pragma: no cover - a stale file must not break a write
        pass


def rerender(
    db: Database,
    cfg: Config,
    note_id: str,
    *,
    folder: str | None = None,
) -> Note:
    """Rebuild the Markdown body from structured data and current links.

    The file follows the note's status and type: an unclear note stays in the
    Inbox, a clear one is promoted.  Moving never changes the user's text; it
    only writes the same authoritative Markdown to the right folder.
    """

    note = get(db, note_id)
    if note is None:
        raise KeyError(note_id)
    content = _content_from(note)
    project = db.query_one("SELECT name FROM projects WHERE id = ?", (note.project_id,)) if note.project_id else None
    project_name = project["name"] if project else None
    target = folder or note_folder(content, project_name=project_name)
    sources = raw.for_note(db, note_id)
    body = render_markdown(
        nid=note.id,
        content=content,
        project_name=project_name,
        tag_names=tags.for_note(db, note_id),
        related=_related_for(db, note_id),
        sources=sources,
        version=note.version,
        created_at=note.created_at,
        updated_at=note.updated_at,
        path_hint=target,
    )
    previous = note.path
    note.body_md = body
    note.path = _write_file(cfg, note, target)
    _remove_old_file(cfg, previous, note.path)
    note.content_hash = _hash_text(note.body_md)
    try:
        note.file_mtime = cfg.note_path(note.path).stat().st_mtime
    except OSError:
        note.file_mtime = 0.0
    db.execute(
        "UPDATE notes SET body_md = ?, path = ?, content_hash = ?, file_mtime = ? WHERE id = ?",
        (note.body_md, note.path, note.content_hash, note.file_mtime, note.id),
    )
    from . import search

    search.fts_upsert(db, note)
    return note


def promote(db: Database, cfg: Config, note_id: str) -> Note:
    """Move a clarified note out of the Inbox when it is ready."""

    note = get(db, note_id)
    if note is None:
        raise KeyError(note_id)
    content = _content_from(note)
    project = db.query_one("SELECT name FROM projects WHERE id = ?", (note.project_id,)) if note.project_id else None
    target = note_folder(content, project_name=project["name"] if project else None)
    current = Path(note.path).parent.as_posix() if note.path else ""
    if target == current:
        return note
    updated = rerender(db, cfg, note_id, folder=target)
    audit.record(
        db,
        "note.promote",
        actor="alan",
        target=note_id,
        details={"from": current, "to": target},
    )
    return updated


def migrate_legacy_paths(db: Database, cfg: Config) -> list[str]:
    """Move note files created before v1.2 into the vault.

    v1.1 stored ``path`` relative to ``data_dir`` (``notes/<slug>.md``).  v1.2
    stores it relative to the vault and organises files by status and type.
    Existing files are moved, not regenerated, so a hand edit survives the
    upgrade.  The operation is idempotent.
    """

    moved: list[str] = []
    rows = db.query("SELECT id FROM notes WHERE path LIKE 'notes/%'")
    for row in rows:
        note = get(db, row["id"])
        if note is None or not note.path:
            continue
        legacy = cfg.data_dir / note.path
        content = _content_from(note)
        project = (
            db.query_one("SELECT name FROM projects WHERE id = ?", (note.project_id,))
            if note.project_id
            else None
        )
        folder = note_folder(content, project_name=project["name"] if project else None)
        target = cfg.vault_dir / folder / f"{note.slug}.md"
        if not legacy.exists() and not note.body_md:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if legacy.exists():
            target.write_bytes(legacy.read_bytes())
            try:
                legacy.unlink()
            except OSError:  # pragma: no cover - the vault copy is authoritative
                pass
        else:
            tmp = target.with_suffix(".md.tmp")
            tmp.write_text(note.body_md, encoding="utf-8", newline="")
            tmp.replace(target)
        relative = target.relative_to(cfg.vault_dir).as_posix()
        text = target.read_text(encoding="utf-8")
        db.execute(
            "UPDATE notes SET path = ?, content_hash = ?, file_mtime = ? WHERE id = ?",
            (relative, _hash_text(text), target.stat().st_mtime, note.id),
        )
        moved.append(relative)
    return moved


def _content_from(note: Note) -> NoteContent:
    data = dict(note.content or {})
    data.setdefault("title", note.title)
    data.setdefault("type", note.type)
    data.setdefault("status", note.status)
    data.setdefault("summary", note.summary)
    data.setdefault("language", note.language)
    data.setdefault("sensitive", note.sensitive)
    data["title"] = note.title
    data["type"] = note.type
    data["status"] = note.status
    data["language"] = note.language
    data["sensitive"] = note.sensitive
    return NoteContent(**{k: v for k, v in data.items() if k in NoteContent.__dataclass_fields__})


def _unique_slug(db: Database, title: str, *, keep: str | None = None) -> str:
    base = slugify(title, fallback="note")
    taken = {row["slug"] for row in db.query("SELECT slug FROM notes")}
    if keep:
        taken.discard(keep)
    return dedupe_suffix(taken, base)


def get(db: Database, note_id: str) -> Note | None:
    row = db.query_one(
        "SELECT n.*, p.name AS project_name FROM notes n "
        "LEFT JOIN projects p ON p.id = n.project_id WHERE n.id = ?",
        (note_id,),
    )
    return Note.from_row(row, tags=tags.for_note(db, note_id)) if row else None


def get_by_slug(db: Database, slug: str) -> Note | None:
    row = db.query_one(
        "SELECT n.*, p.name AS project_name FROM notes n "
        "LEFT JOIN projects p ON p.id = n.project_id WHERE n.slug = ?",
        (slug,),
    )
    return Note.from_row(row, tags=tags.for_note(db, row["id"])) if row else None


def find(db: Database, needle: str) -> Note | None:
    """Resolve an id, exact slug, or unambiguous title fragment."""

    note = get(db, needle) or get_by_slug(db, needle)
    if note is not None:
        return note
    rows = db.query(
        "SELECT * FROM notes WHERE title LIKE ? ORDER BY updated_at DESC LIMIT 2",
        (f"%{needle}%",),
    )
    if len(rows) == 1:
        return get(db, rows[0]["id"])
    return None


def create(
    db: Database,
    cfg: Config,
    content: NoteContent,
    *,
    raw_ids: list[str],
    project_id: str | None = None,
    reason: str = "Created from a raw capture.",
    actor: str = "alan",
    indexed: bool = True,
    slug: str | None = None,
) -> Note:
    nid = new_note_id()
    ts = now_iso()
    # A stable slug lets generated notes (profile, onboarding questions) be
    # found again after a restart or a language switch.
    slug = _unique_slug(db, slug or content.title)

    content.title = one_line(content.title) or "Untitled"
    content.type = content.type if content.type in NOTE_TYPES else "observation"
    content.status = content.status if content.status in NOTE_STATUSES else "active"
    content.summary = content.summary.strip()
    payload = asdict(content)

    db.execute(
        """
        INSERT INTO notes(id, slug, title, type, status, project_id, language, summary,
                          content_json, body_md, path, version, sensitive, indexed,
                          superseded_by, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)
        """,
        (
            nid, slug, content.title, content.type, content.status, project_id,
            content.language, content.summary, json.dumps(payload, ensure_ascii=False),
            "", "", 1, 1 if content.sensitive else 0, 1 if indexed else 0, ts, ts,
        ),
    )

    for raw_id in dict.fromkeys(raw_ids):
        db.execute(
            "INSERT OR IGNORE INTO note_sources(note_id, raw_id, relation) VALUES(?,?, 'source')",
            (nid, raw_id),
        )

    tags.set_for_note(db, nid, content.tags)
    # Inbox first: every capture starts as a working note the user can see.
    note = rerender(db, cfg, nid, folder=INBOX)
    project_row = (
        db.query_one("SELECT name FROM projects WHERE id = ?", (project_id,))
        if project_id
        else None
    )
    final = note_folder(content, project_name=project_row["name"] if project_row else None)
    if final != INBOX:
        note = rerender(db, cfg, nid, folder=final)
        audit.record(
            db,
            "note.promote",
            actor=actor,
            target=nid,
            details={"from": INBOX, "to": final},
        )

    db.execute(
        "INSERT INTO note_versions(note_id, version, title, body_md, summary, reason, raw_id, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (nid, note.version, note.title, note.body_md, note.summary, reason,
         raw_ids[0] if raw_ids else None, ts),
    )
    audit.record(
        db,
        "note.create",
        actor=actor,
        target=nid,
        details={"title": note.title, "type": note.type, "raw_ids": raw_ids},
    )
    return note


def update_content(
    db: Database,
    cfg: Config,
    note_id: str,
    content: NoteContent,
    *,
    raw_ids: list[str] | None = None,
    project_id: str | None = None,
    reason: str = "Updated.",
    actor: str = "alan",
) -> Note:
    note = get(db, note_id)
    if note is None:
        raise KeyError(note_id)

    old_version = note.version
    new_version = old_version + 1
    content.title = one_line(content.title) or note.title
    content.type = content.type if content.type in NOTE_TYPES else note.type
    content.status = content.status if content.status in NOTE_STATUSES else note.status
    ts = now_iso()

    db.execute(
        """
        UPDATE notes SET title = ?, type = ?, status = ?, project_id = ?, language = ?,
               summary = ?, content_json = ?, version = ?, sensitive = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            content.title, content.type, content.status,
            project_id if project_id is not None else note.project_id,
            content.language, content.summary,
            json.dumps(asdict(content), ensure_ascii=False),
            new_version, 1 if content.sensitive else 0, ts, note_id,
        ),
    )

    for raw_id in dict.fromkeys(raw_ids or []):
        db.execute(
            "INSERT OR IGNORE INTO note_sources(note_id, raw_id, relation) VALUES(?,?, 'update')",
            (note_id, raw_id),
        )

    tags.set_for_note(db, note_id, content.tags)
    updated = rerender(db, cfg, note_id)

    db.execute(
        "INSERT INTO note_versions(note_id, version, title, body_md, summary, reason, raw_id, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (note_id, new_version, updated.title, updated.body_md, updated.summary, reason,
         (raw_ids or [None])[0], ts),
    )
    audit.record(
        db,
        "note.update",
        actor=actor,
        target=note_id,
        details={"from_version": old_version, "to_version": new_version, "reason": reason},
    )
    return updated


def set_status(db: Database, note_id: str, status: str, *, actor: str = "user") -> None:
    db.execute("UPDATE notes SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), note_id))
    audit.record(db, "note.status", actor=actor, target=note_id, details={"status": status})


def set_private(db: Database, note_id: str, sensitive: bool, *, actor: str = "user") -> None:
    db.execute(
        "UPDATE notes SET sensitive = ?, updated_at = ? WHERE id = ?",
        (1 if sensitive else 0, now_iso(), note_id),
    )
    audit.record(db, "note.privacy", actor=actor, target=note_id, details={"sensitive": sensitive})
    from . import search

    search.fts_delete(db, note_id)
    note = get(db, note_id)
    if note and not sensitive:
        search.fts_upsert(db, note)


def set_indexed(db: Database, note_id: str, indexed: bool, *, actor: str = "user") -> None:
    db.execute("UPDATE notes SET indexed = ? WHERE id = ?", (1 if indexed else 0, note_id))
    audit.record(db, "note.index", actor=actor, target=note_id, details={"indexed": indexed})
    from . import search

    if indexed:
        note = get(db, note_id)
        if note:
            search.fts_upsert(db, note)
    else:
        search.fts_delete(db, note_id)
        db.execute("DELETE FROM embeddings WHERE note_id = ?", (note_id,))


def delete(db: Database, cfg: Config, note_id: str, *, actor: str = "user") -> bool:
    note = get(db, note_id)
    if note is None:
        return False
    from . import search

    search.fts_delete(db, note_id)
    db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    path = cfg.note_path(note.path)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    audit.record(db, "note.delete", actor=actor, target=note_id, details={"title": note.title})
    return True


def versions(db: Database, note_id: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT version, title, summary, reason, created_at FROM note_versions "
        "WHERE note_id = ? ORDER BY version DESC",
        (note_id,),
    )
    return [dict(row) for row in rows]


def version_body(db: Database, note_id: str, version: int) -> str | None:
    row = db.query_one(
        "SELECT body_md FROM note_versions WHERE note_id = ? AND version = ?",
        (note_id, version),
    )
    return row["body_md"] if row else None


def list_notes(
    db: Database,
    *,
    limit: int = 20,
    offset: int = 0,
    type: str | None = None,
    status: str | None = None,
    project_id: str | None = None,
    include_sensitive: bool = True,
) -> list[Note]:
    sql = (
        "SELECT n.*, p.name AS project_name FROM notes n "
        "LEFT JOIN projects p ON p.id = n.project_id WHERE 1=1"
    )
    params: list[object] = []
    if type:
        sql += " AND n.type = ?"
        params.append(type)
    if status:
        sql += " AND n.status = ?"
        params.append(status)
    if project_id:
        sql += " AND n.project_id = ?"
        params.append(project_id)
    if not include_sensitive:
        sql += " AND n.sensitive = 0"
    sql += " ORDER BY n.updated_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return [Note.from_row(row, tags=tags.for_note(db, row["id"])) for row in db.query(sql, params)]


def due_items(db: Database) -> list[Note]:
    """Notes whose deadline text parses to a date within the recent past/future."""

    out: list[Note] = []
    for note in list_notes(db, limit=500, status="active"):
        for deadline in note.content.get("deadlines", []) or []:
            try:
                dt = parse_iso(str(deadline))
            except (ValueError, TypeError):
                continue
            out.append(note)
            break
    return out


def stats(db: Database) -> dict[str, int]:
    return {
        "notes": int(db.scalar("SELECT COUNT(*) FROM notes") or 0),
        "active": int(db.scalar("SELECT COUNT(*) FROM notes WHERE status = 'active'") or 0),
        "sensitive": int(db.scalar("SELECT COUNT(*) FROM notes WHERE sensitive = 1") or 0),
        "raw": raw.count(db),
        "projects": int(db.scalar("SELECT COUNT(*) FROM projects WHERE confirmed = 1") or 0),
        "links": links.count(db),
        "conflicts": conflicts.count_unresolved(db),
        "open_questions": int(
            db.scalar("SELECT COUNT(*) FROM notes WHERE type = 'question' AND status = 'unresolved'") or 0
        ),
    }
