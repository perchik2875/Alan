"""Validated schemas for everything the reasoning model is asked to produce.

All model output passes through pydantic before it can touch storage.  This is
what keeps a weaker or more creative model from corrupting the knowledge base.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Intent = Literal[
    "capture", "question", "find", "overview", "correction",
    "feedback", "command", "chitchat",
]
Basis = Literal["fact", "inference", "assumption", "unknown"]
# How durable a stated instruction is.  "unclear" obliges Alan to ask before
# turning it into a lesson; "none" forbids a lesson entirely.
DurableIntent = Literal["none", "one_time", "durable", "unclear"]


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return [str(value)]
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text[:2000])
    return out


class NoteDraft(BaseModel):
    title: str = ""
    type: str | None = None
    status: str | None = None
    summary: str = ""
    decision: str = ""
    reasoning: str = ""
    context: str = ""
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    deadlines: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    project: str | None = None
    project_is_new: bool = False
    sensitive: bool | None = None
    updates_note_id: str | None = None

    _lists = field_validator(
        "constraints", "assumptions", "alternatives", "open_questions",
        "next_actions", "deadlines", "tags", mode="before",
    )(_strings)

    @field_validator("title", "summary", "decision", "reasoning", "context", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("project", "updates_note_id", mode="before")
    @classmethod
    def _optional(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class LinkProposal(BaseModel):
    note_id: str
    reason: str = ""
    confidence: float = 0.6

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.6
        return min(1.0, max(0.0, number))


class ConflictFinding(BaseModel):
    note_id: str
    existing_claim: str = ""
    new_claim: str = ""
    explanation: str = ""


class Correction(BaseModel):
    note_id: str | None = None
    changes: dict[str, Any] = Field(default_factory=dict)
    lesson: str = ""
    lesson_kind: str = "interpretation"


class LessonCandidate(BaseModel):
    """A proposed behavioural rule, in English, with provenance."""

    text: str = ""
    kind: str = "preference"
    scope: str = "global"
    confidence: float = 0.6
    explanation: str = ""

    @field_validator("text", "explanation", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, value: Any) -> float:
        try:
            return min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return 0.6


class PreferenceCandidate(BaseModel):
    key: str = ""
    value: str = ""
    explanation: str = ""

    @field_validator("key", "value", "explanation", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()


class Assessment(BaseModel):
    intent: Intent = "capture"
    language: str = ""
    needs_clarification: bool = False
    clarifying_questions: list[str] = Field(default_factory=list)
    note: NoteDraft | None = None
    search_query: str = ""
    links: list[LinkProposal] = Field(default_factory=list)
    conflicts: list[ConflictFinding] = Field(default_factory=list)
    correction: Correction | None = None
    assumptions: list[str] = Field(default_factory=list)
    reply: str = ""
    confidence: float = 1.0
    durable_intent: DurableIntent = "none"
    lesson: LessonCandidate | None = None
    preference: PreferenceCandidate | None = None
    memory_refs: list[str] = Field(default_factory=list)

    _list_questions = field_validator(
        "clarifying_questions", "assumptions", "memory_refs", mode="before"
    )(_strings)

    @field_validator("reply", mode="before")
    @classmethod
    def _reply(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, value: Any) -> float:
        try:
            return min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return 1.0


class Evidence(BaseModel):
    statement: str = ""
    basis: Basis = "fact"
    note_ids: list[str] = Field(default_factory=list)

    _ids = field_validator("note_ids", mode="before")(_strings)


class AnswerDraft(BaseModel):
    answer: str = ""
    sources: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)

    _lists = field_validator("sources", "unresolved", mode="before")(_strings)


class LessonDraft(BaseModel):
    kind: str = "interpretation"
    text: str = ""
    scope: str = "global"
    confidence: float = 0.6
    explanation: str = ""

    @field_validator("text", "explanation", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, value: Any) -> float:
        try:
            return min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return 0.6


class ReflectionDraft(BaseModel):
    lessons: list[LessonDraft] = Field(default_factory=list)
    summary: str = ""


class Theme(BaseModel):
    name: str = ""
    description: str = ""
    note_count: int = 0


class InventoryDraft(BaseModel):
    summary: str = ""
    themes: list[Theme] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    active: list[str] = Field(default_factory=list)
    stale: list[str] = Field(default_factory=list)
    duplicates: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    missing_context: list[str] = Field(default_factory=list)
    suggested_projects: list[str] = Field(default_factory=list)
    first_step: str = ""


class ProactiveDraft(BaseModel):
    worth_sending: bool = False
    message: str = ""
    note_ids: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)

    _ids = field_validator("note_ids", "kinds", mode="before")(_strings)
