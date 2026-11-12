"""Deterministic application of a validated assessment to storage.

The model proposes; this module decides what actually happens.  Keeping this
step free of model calls means the rules for versioning, linking, conflict
recording, and project confirmation are identical for every provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..db import Database
from ..models import Link, Note, NoteContent, Project
from ..store import conflicts as conflicts_store
from ..store import lessons as lessons_store
from ..store import links as links_store
from ..store import notes as notes_store
from ..store import projects as projects_store
from ..textutil import detect_language, one_line, title_from_text
from .schemas import Assessment, NoteDraft

_META_FIELDS = {"title", "type", "status", "sensitive", "language"}
_LIST_FIELDS = {
    "constraints", "assumptions", "alternatives",
    "open_questions", "next_actions", "deadlines", "tags",
}
_TEXT_FIELDS = {"summary", "decision", "reasoning", "context"}


@dataclass(slots=True)
class ApplyResult:
    note: Note | None = None
    created: bool = False
    updated: bool = False
    project: Project | None = None
    project_is_new: bool = False
    confirmed_links: list[Link] = field(default_factory=list)
    proposed_links: list[Link] = field(default_factory=list)
    conflicts: list[object] = field(default_factory=list)
    lesson: object | None = None
    skipped_reason: str = ""


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(item.strip())
    return out


def _draft_has_substance(draft: NoteDraft) -> bool:
    if any(
        one_line(getattr(draft, name))
        for name in ("summary", "decision", "reasoning", "context")
    ):
        return True
    return any(getattr(draft, name) for name in _LIST_FIELDS - {"tags"})


def draft_to_content(
    draft: NoteDraft,
    *,
    language: str,
    base: NoteContent | None = None,
) -> NoteContent:
    """Build or merge a NoteContent from a model draft.

    With ``base`` supplied the draft is treated as a patch: empty fields keep
    the existing value and lists are unioned rather than replaced.
    """

    if base is None:
        content = NoteContent(
            title=one_line(draft.title) or "Untitled",
            type=draft.type or "observation",
            status=draft.status or "active",
            language=language,
            summary=draft.summary,
            decision=draft.decision,
            reasoning=draft.reasoning,
            context=draft.context,
            constraints=list(draft.constraints),
            assumptions=list(draft.assumptions),
            alternatives=list(draft.alternatives),
            open_questions=list(draft.open_questions),
            next_actions=list(draft.next_actions),
            deadlines=list(draft.deadlines),
            tags=_dedupe(draft.tags),
            sensitive=bool(draft.sensitive),
        )
        return content

    from dataclasses import replace

    content = replace(
        base,
        constraints=list(base.constraints),
        assumptions=list(base.assumptions),
        alternatives=list(base.alternatives),
        open_questions=list(base.open_questions),
        next_actions=list(base.next_actions),
        deadlines=list(base.deadlines),
        tags=list(base.tags),
    )

    if one_line(draft.title):
        content.title = one_line(draft.title)
    if draft.type:
        content.type = draft.type
    if draft.status:
        content.status = draft.status
    if draft.sensitive is not None:
        content.sensitive = bool(draft.sensitive)

    for name in _TEXT_FIELDS:
        value = getattr(draft, name)
        if one_line(value):
            setattr(content, name, value)

    for name in _LIST_FIELDS:
        merged = list(getattr(content, name)) + list(getattr(draft, name))
        setattr(content, name, _dedupe(merged))

    return content


def apply_changes(base: NoteContent, changes: dict[str, object]) -> NoteContent:
    """Apply a correction patch, ignoring unknown or malformed fields."""

    from dataclasses import replace

    updated = replace(
        base,
        constraints=list(base.constraints),
        assumptions=list(base.assumptions),
        alternatives=list(base.alternatives),
        open_questions=list(base.open_questions),
        next_actions=list(base.next_actions),
        deadlines=list(base.deadlines),
        tags=list(base.tags),
    )
    for key, value in changes.items():
        if key not in NoteContent.__dataclass_fields__:
            continue
        if key in _LIST_FIELDS:
            if isinstance(value, str):
                value = [value]
            if isinstance(value, (list, tuple)):
                setattr(updated, key, _dedupe([str(v) for v in value if str(v).strip()]))
        elif key == "sensitive":
            updated.sensitive = bool(value)
        elif key == "language":
            if isinstance(value, str) and value.strip():
                updated.language = value.strip()
        else:
            text = one_line(str(value or ""))
            if text:
                setattr(updated, key, text)
    return updated


def _existing_content(note: Note) -> NoteContent:
    return notes_store._content_from(note)


def _resolve_project(db: Database, draft: NoteDraft) -> tuple[Project | None, bool]:
    name = one_line(draft.project or "")
    if not name:
        return None, False

    found = projects_store.find_by_name(db, name)
    if found is not None:
        return found, False

    # A project that did not exist is always proposed, never imposed.  The
    # service asks the user to confirm the name before it becomes permanent.
    project = projects_store.create(db, name, confirmed=False)
    return project, True


def apply_assessment(
    db: Database,
    cfg: Config,
    assessment: Assessment,
    *,
    raw_id: str | None,
    raw_ids: list[str] | None = None,
    actor: str = "alan",
) -> ApplyResult:
    result = ApplyResult()
    source_ids = [rid for rid in dict.fromkeys((raw_ids or []) + ([raw_id] if raw_id else [])) if rid]

    if assessment.intent == "correction" and assessment.correction is not None:
        target = None
        if assessment.correction.note_id:
            target = notes_store.get(db, assessment.correction.note_id) or notes_store.find(
                db, assessment.correction.note_id
            )
        if target is None and assessment.note and assessment.note.updates_note_id:
            target = notes_store.get(db, assessment.note.updates_note_id)

        if target is not None:
            base = _existing_content(target)
            merged = apply_changes(base, assessment.correction.changes)
            if assessment.note is not None:
                merged = draft_to_content(assessment.note, language=base.language, base=merged)
            project = target.project_id
            note = notes_store.update_content(
                db, cfg, target.id, merged,
                raw_ids=source_ids,
                project_id=project,
                reason=assessment.correction.lesson or "Corrected by the user.",
                actor=actor,
            )
            result.note = note
            result.updated = True
            if assessment.correction.lesson:
                result.lesson = lessons_store.add(
                    db,
                    kind=assessment.correction.lesson_kind or "interpretation",
                    text=assessment.correction.lesson,
                    evidence_note_id=note.id,
                    evidence_raw_id=raw_id,
                    actor=actor,
                )
            _apply_links_and_conflicts(db, cfg, assessment, note, raw_id, result, actor=actor)
            return result
        result.skipped_reason = "correction-target-missing"
        # Fall through: if the message still contains durable substance, it is
        # recorded as a new note rather than lost.

    draft = assessment.note
    if assessment.needs_clarification or draft is None or not _draft_has_substance(draft):
        result.skipped_reason = "clarification" if assessment.needs_clarification else "no durable substance"
        return result

    project, project_is_new = _resolve_project(db, draft)
    result.project = project
    result.project_is_new = project_is_new and project is not None and not project.confirmed

    existing = None
    if draft.updates_note_id:
        existing = notes_store.get(db, draft.updates_note_id)

    language = assessment.language or detect_language(draft.summary or draft.title, cfg.default_language)

    if existing is not None:
        base = _existing_content(existing)
        merged = draft_to_content(draft, language=base.language, base=base)
        note = notes_store.update_content(
            db, cfg, existing.id, merged,
            raw_ids=source_ids,
            project_id=project.id if project else existing.project_id,
            reason="Updated from a new message.",
            actor=actor,
        )
        result.note = note
        result.updated = True
    else:
        content = draft_to_content(draft, language=language)
        if not one_line(draft.title):
            content.title = title_from_text(content.summary or content.decision or "Untitled")
        note = notes_store.create(
            db, cfg, content,
            raw_ids=source_ids,
            project_id=project.id if project else None,
            reason="Created from a raw capture.",
            actor=actor,
        )
        result.note = note
        result.created = True

    _apply_links_and_conflicts(db, cfg, assessment, note, raw_id, result, actor=actor)
    return result


def _apply_links_and_conflicts(
    db: Database,
    cfg: Config,
    assessment: Assessment,
    note: Note,
    raw_id: str | None,
    result: ApplyResult,
    *,
    actor: str,
) -> None:
    for proposal in assessment.links:
        target = notes_store.get(db, proposal.note_id)
        if target is None:
            continue
        link = links_store.create(
            db,
            note.id,
            target.id,
            reason=proposal.reason,
            confidence=proposal.confidence,
            origin="alan",
        )
        if link is None:
            continue
        # Re-render so the new relationship appears in the Markdown file.
        notes_store.rerender(db, cfg, note.id)
        if link.status == "confirmed":
            result.confirmed_links.append(link)
        else:
            result.proposed_links.append(link)

    for finding in assessment.conflicts:
        other = notes_store.get(db, finding.note_id)
        if other is None:
            continue
        conflict = conflicts_store.create(
            db,
            note_id=note.id,
            other_note_id=other.id,
            existing_claim=finding.existing_claim or other.summary,
            new_claim=finding.new_claim or note.summary,
            explanation=finding.explanation,
            raw_id=raw_id,
        )
        if conflict is not None:
            result.conflicts.append(conflict)
