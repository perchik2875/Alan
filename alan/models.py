"""Record types shared by the store and the reasoning layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(slots=True)
class RawCapture:
    id: str
    chat_id: str
    message_id: int | None
    received_at: str
    source: str
    language: str
    text: str
    text_format: str = "text"
    entities: list[dict[str, Any]] = field(default_factory=list)
    media: list[dict[str, Any]] = field(default_factory=list)
    reply_to_id: str | None = None
    conversation_context: str | None = None
    content_hash: str = ""
    path: str = ""
    imported: bool = False
    created_at: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "RawCapture":
        import json

        return cls(
            id=row["id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            received_at=row["received_at"],
            source=row["source"],
            language=row["language"],
            text=row["text"],
            text_format=row["text_format"],
            entities=json.loads(row["entities_json"] or "[]"),
            media=json.loads(row["media_json"] or "[]"),
            reply_to_id=row["reply_to_id"],
            conversation_context=row["conversation_context"],
            content_hash=row["content_hash"],
            path=row["path"],
            imported=bool(row["imported"]),
            created_at=row["created_at"],
        )


@dataclass(slots=True)
class Note:
    id: str
    slug: str
    title: str
    type: str
    status: str
    project_id: str | None
    language: str
    summary: str
    content: dict[str, Any]
    body_md: str
    path: str
    version: int
    sensitive: bool
    indexed: bool
    superseded_by: str | None
    created_at: str
    updated_at: str
    tags: list[str] = field(default_factory=list)
    project_name: str | None = None
    content_hash: str = ""
    file_mtime: float = 0.0

    @classmethod
    def from_row(cls, row: Mapping[str, Any], *, tags: list[str] | None = None) -> "Note":
        import json

        keys = set(row.keys())
        return cls(
            id=row["id"],
            slug=row["slug"],
            title=row["title"],
            type=row["type"],
            status=row["status"],
            project_id=row["project_id"],
            language=row["language"],
            summary=row["summary"],
            content=json.loads(row["content_json"] or "{}"),
            body_md=row["body_md"],
            path=row["path"],
            version=row["version"],
            sensitive=bool(row["sensitive"]),
            indexed=bool(row["indexed"]),
            superseded_by=row["superseded_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tags=list(tags or []),
            project_name=row["project_name"] if "project_name" in keys else None,
            content_hash=row["content_hash"] if "content_hash" in keys else "",
            file_mtime=float(row["file_mtime"]) if "file_mtime" in keys else 0.0,
        )


@dataclass(slots=True)
class NoteContent:
    """The curated substance of a note, independent of storage details."""

    title: str
    type: str
    status: str
    summary: str = ""
    decision: str = ""
    reasoning: str = ""
    context: str = ""
    constraints: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    deadlines: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    language: str = "en"
    sensitive: bool = False


@dataclass(slots=True)
class Project:
    id: str
    name: str
    slug: str
    description: str
    status: str
    confirmed: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Project":
        return cls(
            id=row["id"],
            name=row["name"],
            slug=row["slug"],
            description=row["description"],
            status=row["status"],
            confirmed=bool(row["confirmed"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(slots=True)
class Link:
    id: str
    from_note_id: str
    to_note_id: str
    reason: str
    confidence: float
    status: str
    origin: str
    created_at: str


@dataclass(slots=True)
class Conflict:
    id: str
    note_id: str
    other_note_id: str
    existing_claim: str
    new_claim: str
    explanation: str
    status: str
    resolution: str
    resolved_at: str | None
    raw_id: str | None
    detected_at: str


@dataclass(slots=True)
class Lesson:
    id: str
    kind: str
    text: str
    scope: str
    evidence_note_id: str | None
    evidence_raw_id: str | None
    active: bool
    times_applied: int
    created_at: str
    confidence: float = 0.6
    explanation: str = ""
    last_applied_at: str | None = None
    success_count: int = 0
    failure_count: int = 0


@dataclass(slots=True)
class Reminder:
    id: str
    chat_id: str
    note_id: str | None
    kind: str
    message: str
    due_at: str
    status: str
    origin: str
    created_at: str
    last_sent_at: str | None
    sent_count: int


@dataclass(slots=True)
class SearchHit:
    note: Note
    score: float
    methods: tuple[str, ...] = ()
    fts_rank: float = 0.0
    vector_score: float = 0.0
