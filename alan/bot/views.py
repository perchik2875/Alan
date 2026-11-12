"""Rendering for Telegram.

Plain text everywhere: user content must never be interpreted as markup.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from ..models import Conflict, Note, Project, RawCapture
from ..textutil import clamp, human_datetime

MAX_MESSAGE = 3900


def chunk(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit and current:
            chunks.append("".join(current).rstrip())
            current, size = [], 0
        if len(line) > limit:
            for index in range(0, len(line), limit):
                chunks.append(line[index : index + limit])
            continue
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current).rstrip())
    return chunks or [""]


def note_card(note: Note, *, limit: int = 3400) -> str:
    content = note.content or {}
    lines = [f"{note.title}"]
    meta = [note.type, note.status]
    if note.project_name:
        meta.append(note.project_name)
    if note.sensitive:
        meta.append("private")
    lines.append(" · ".join(meta))
    lines.append(f"id: {note.id} · v{note.version} · updated {note.updated_at[:10]}")

    if note.summary:
        lines += ["", note.summary]
    if content.get("decision") and content["decision"] != note.summary:
        lines += ["", f"Decision: {content['decision']}"]
    if content.get("reasoning"):
        lines += ["", f"Reasoning: {content['reasoning']}"]
    if content.get("context"):
        lines += ["", f"Context: {content['context']}"]

    for key, label in (
        ("constraints", "Constraints"),
        ("assumptions", "Assumptions"),
        ("alternatives", "Alternatives"),
        ("open_questions", "Open questions"),
        ("next_actions", "Next actions"),
        ("deadlines", "Deadlines"),
    ):
        values = content.get(key) or []
        if values:
            lines.append("")
            lines.append(f"{label}:")
            lines.extend(f"- {value}" for value in values)

    if note.tags:
        lines += ["", "Tags: " + ", ".join(f"#{t}" for t in note.tags)]

    if note.superseded_by:
        lines += ["", f"Superseded by: {note.superseded_by}"]

    return clamp("\n".join(lines), limit)


def note_links(related: Sequence[dict[str, str]]) -> str:
    if not related:
        return ""
    return "\n".join(f"- {item['slug']} — {item['title']}" for item in related)


def note_sources(captures: Sequence[RawCapture]) -> str:
    if not captures:
        return ""
    return "\n".join(
        f"- {human_datetime(cap.received_at)} · {cap.path} ({len(cap.text)} chars)"
        for cap in captures
    )


def note_list(notes: Iterable[Note], *, header: str = "") -> str:
    rows = [f"{note.title} · {note.type}/{note.status} · {note.slug}" for note in notes]
    if not rows:
        return (header + "\n" if header else "") + "Nothing recorded yet, sir."
    return (header + "\n" if header else "") + "\n".join(f"- {row}" for row in rows)


def project_list(projects: Sequence[Project], counts: dict[str, int] | None = None) -> str:
    if not projects:
        return "No projects yet, sir."
    lines = []
    for project in projects:
        count = (counts or {}).get(project.id)
        suffix = f" · {count} notes" if count is not None else ""
        lines.append(f"- {project.name}{suffix}" + ("" if project.confirmed else " (proposed)"))
    return "\n".join(lines)


def conflict_card(conflict: Conflict, new_note: Note | None, old_note: Note | None) -> str:
    return (
        "Conflict detected, sir.\n\n"
        f"{old_note.title if old_note else '(unknown)'}: “{conflict.existing_claim}”\n"
        f"{new_note.title if new_note else '(unknown)'}: “{conflict.new_claim}”\n\n"
        f"{conflict.explanation}\n\nWhich reflects your current position?"
    )


def stats_block(stats: dict[str, int]) -> str:
    return (
        "Knowledge base, sir:\n"
        f"- notes: {stats.get('notes', 0)} ({stats.get('active', 0)} active, {stats.get('sensitive', 0)} private)\n"
        f"- raw captures: {stats.get('raw', 0)}\n"
        f"- projects: {stats.get('projects', 0)}\n"
        f"- links: {stats.get('links', 0)}\n"
        f"- unresolved conflicts: {stats.get('conflicts', 0)}\n"
        f"- open questions: {stats.get('open_questions', 0)}"
    )
