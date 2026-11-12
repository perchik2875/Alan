"""Prompt composition from the Markdown prompt library.

Behavioural prompt text is not stored in Python.  ``PromptSet`` composes the
system prompts from the validated Markdown files; the model is a replaceable
reasoning component and the composed prompts are identical for every provider.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from ..models import Note, Project, SearchHit
from ..textutil import one_line, clamp
from .prompt_loader import PromptLibrary, packaged_prompts_dir

_DEFAULT: PromptLibrary | None = None


def default_library() -> PromptLibrary:
    """The packaged library, used when no vault-specific library is supplied."""

    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = PromptLibrary(packaged_prompts_dir())
        _DEFAULT.ensure()
    return _DEFAULT


class PromptSet:
    """Compose the provider-neutral system prompts from Markdown files."""

    def __init__(self, library: PromptLibrary | None = None) -> None:
        self.library = library or default_library()

    def _text(self, *names: str) -> str:
        return "\n\n".join(self.library.file(name).text.strip() for name in names)

    def persona(self) -> str:
        return self._text("persona")

    def assess_system(self) -> str:
        return self._text("persona", "capture", "clarification", "conflict", "learning", "safety")

    def answer_system(self) -> str:
        return self._text("persona", "retrieval", "conflict", "safety")

    def proactive_system(self) -> str:
        return self._text("persona", "proactive", "safety")

    def reflection_system(self) -> str:
        return self._text("persona", "learning", "safety")

    def inventory_system(self) -> str:
        return self._text("persona", "capture", "safety")

    # -- prompt sections used by deterministic features ----------------

    def onboarding_section(self, section: str, **values: object) -> str:
        return self.library.render("onboarding", section=section, **values)

    def onboarding_questions(self, language: str) -> list[str]:
        key = "Questions (ru)" if (language or "").lower().startswith(("ru", "uk")) else "Questions (en)"
        lines = []
        for line in self.library.section("onboarding", key).splitlines():
            stripped = line.strip()
            if stripped.startswith(("- ", "* ")):
                question = stripped[2:].strip()
                if question:
                    lines.append(question)
        return lines

    def safety_text(self) -> str:
        return self.library.file("safety").text



# --------------------------------------------------------------------------
# Context rendering
# --------------------------------------------------------------------------

def _project_list(projects: Sequence[Project]) -> str:
    if not projects:
        return "(none yet)"
    return "\n".join(
        f"- {p.name}" + ("" if p.confirmed else " (proposed, not yet confirmed)") for p in projects
    )


def render_candidates(hits: Sequence[SearchHit]) -> str:
    if not hits:
        return "(nothing relevant found)"
    blocks: list[str] = []
    for hit in hits:
        note = hit.note
        project = f" | project: {note.project_name}" if note.project_name else ""
        tags = f" | tags: {', '.join(note.tags)}" if note.tags else ""
        blocks.append(
            f"- id: {note.id} | {note.type}/{note.status}{project}{tags}\n"
            f"  title: {note.title}\n"
            f"  summary: {one_line(note.summary) or one_line(note.body_md)[:240]}"
        )
    return "\n".join(blocks)


def render_history(history: Sequence[dict[str, str]], *, limit: int = 30) -> str:
    if not history:
        return "(no recent conversation)"
    lines = []
    for turn in list(history)[-limit:]:
        role = "User" if turn.get("role") == "user" else "Alan"
        lines.append(f"{role}: {clamp(one_line(turn.get('text', '')), 500)}")
    return "\n".join(lines)


def render_note(note: Note, *, limit: int = 3500) -> str:
    content = note.content or {}
    parts = [
        f"### id: {note.id} | {note.type}/{note.status}" + (f" | project: {note.project_name}" if note.project_name else ""),
        f"Title: {note.title}",
        f"Updated: {note.updated_at[:10]} | Created: {note.created_at[:10]}",
    ]
    if note.tags:
        parts.append("Tags: " + ", ".join(note.tags))
    if note.superseded_by:
        parts.append(f"Superseded by: {note.superseded_by}")
    if note.sensitive:
        parts.append("MARKED SENSITIVE")
    for key, label in (
        ("summary", "Summary"),
        ("decision", "Decision"),
        ("reasoning", "Reasoning"),
        ("context", "Context"),
    ):
        value = content.get(key)
        if value:
            parts.append(f"{label}: {value}")
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
            parts.append(f"{label}: " + "; ".join(str(v) for v in values))
    text = "\n".join(parts)
    return clamp(text, limit)


def render_notes(notes: Iterable[Note], *, limit: int = 3500) -> str:
    blocks = [render_note(note, limit=limit) for note in notes]
    return "\n\n".join(blocks) if blocks else "(no matching notes)"


def render_conflicts(rows: Sequence[dict[str, object]]) -> str:
    if not rows:
        return "(none)"
    lines = []
    for row in rows:
        lines.append(
            f"- {row.get('note_title')} ({row.get('note_id')}) says: {row.get('new_claim')} | "
            f"{row.get('other_title')} ({row.get('other_note_id')}) says: {row.get('existing_claim')} "
            f"— {row.get('explanation')}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# User-message builders
# --------------------------------------------------------------------------

def assess_user(
    *,
    text: str,
    language: str,
    now: str,
    timezone: str,
    projects: Sequence[Project],
    candidates: Sequence[SearchHit],
    history: Sequence[dict[str, str]],
    pending_questions: Sequence[str] | None = None,
    original_text: str | None = None,
    user_preferences: str = "",
    lessons: str = "",
) -> str:
    blocks = [
        f"Current time: {now} ({timezone}).",
        f"Detected language: {language}.",
        "",
        "Existing projects:",
        _project_list(projects),
        "",
        "Candidate notes already in the knowledge base:",
        render_candidates(candidates),
        "",
        "Current conversation (working memory, persisted):",
        render_history(history),
        "",
        "The latest message is the one you must classify. Use the conversation to resolve references such as "
        "\"that\", \"it\", \"the previous message\", or a correction of your last answer.",
    ]
    if user_preferences:
        blocks += ["", "User preferences:", user_preferences]
    if lessons:
        blocks += ["", "Lessons learned from earlier corrections:", lessons]
    if pending_questions:
        blocks += [
            "",
            "You previously asked these clarifying questions:",
            "\n".join(f"- {q}" for q in pending_questions),
            "",
            f"Original message: {original_text}",
            "",
            "The user's reply answers them. Combine the original message and this reply into one complete note. Do not ask the same questions again.",
        ]
    blocks += ["", "User's latest message:", text]
    return "\n".join(blocks)


def answer_user(
    *,
    question: str,
    notes: Sequence[Note],
    conflicts: Sequence[dict[str, object]],
    history: Sequence[dict[str, str]] | None = None,
    lessons: str = "",
    preferences: str = "",
    detail: str = "normal",
    language: str = "en",
) -> str:
    blocks = [
        f"The user's question: {question}",
        f"Reply language: {language}.",
        f"Requested detail level: {detail}.",
        "",
        "Current conversation (working memory, persisted):",
        render_history(history or []),
        "",
        "Relevant notes:",
        render_notes(notes),
        "",
        "Unresolved conflicts that touch these notes:",
        render_conflicts(conflicts),
    ]
    if preferences:
        blocks += ["", "User preferences:", preferences]
    if lessons:
        blocks += ["", "Lessons learned from earlier corrections:", lessons]
    return "\n".join(blocks)


def proactive_user(*, now: str, candidates: str, recent: str, last_sent: str | None) -> str:
    blocks = [
        f"Current time: {now}.",
        f"Last proactive message: {last_sent or 'never'}.",
        "",
        "Candidate items derived from the knowledge base:",
        candidates or "(none)",
        "",
        "Recently surfaced (do not repeat):",
        recent or "(nothing)",
    ]
    return "\n".join(blocks)


def reflection_user(*, transcript: str) -> str:
    return (
        "Recent interaction transcript:\n\n"
        f"{transcript}\n\n"
        "Extract only durable, generalisable lessons supported by this transcript."
    )


def inventory_user(*, items: str, stats: str) -> str:
    return (
        f"Imported material statistics:\n{stats}\n\n"
        f"Sample of imported items:\n{items}"
    )
