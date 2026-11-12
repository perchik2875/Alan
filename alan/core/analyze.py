"""The reasoning step.

One place where the provider-neutral LLM is called, one place where its output
is validated, and one repair attempt when a model produces malformed JSON.
Nothing else in Alan talks to a model directly.
"""

from __future__ import annotations

from typing import Sequence

from ..config import LLMConfig
from ..errors import LLMError, LLMOutputInvalid
from ..llm.base import LLMClient, Message
from ..llm.jsonx import extract_json, validate
from ..models import Note, Project, SearchHit
from . import prompts
from .prompts import PromptSet
from .schemas import (
    AnswerDraft,
    Assessment,
    InventoryDraft,
    ProactiveDraft,
    ReflectionDraft,
)


class Reasoner:
    def __init__(self, llm: LLMClient, cfg: LLMConfig, *, prompts_set: PromptSet | None = None) -> None:
        self.llm = llm
        self.cfg = cfg
        self.prompts = prompts_set or PromptSet()

    async def _structured(self, model, *, system: str, user: str, max_tokens: int | None = None):
        """Ask for JSON, validate, and retry once with the error attached."""

        attempt_messages = [Message(role="user", content=user)]
        last_error: Exception | None = None

        for attempt in range(2):
            response = await self.llm.complete(
                system=system,
                messages=attempt_messages,
                max_tokens=max_tokens or self.cfg.max_tokens,
                temperature=self.cfg.temperature if attempt == 0 else 0.0,
                json_mode=True,
            )
            try:
                return validate(model, extract_json(response.text))
            except LLMOutputInvalid as exc:
                last_error = exc
                attempt_messages = [
                    Message(role="user", content=user),
                    Message(role="assistant", content=response.text[:4000]),
                    Message(
                        role="user",
                        content=(
                            "That response could not be used: "
                            f"{exc}. Return one valid JSON object with the required keys. "
                            "No prose, no code fences, no comments."
                        ),
                    ),
                ]
        raise LLMOutputInvalid(
            f"the model did not produce valid JSON after a repair attempt: {last_error}"
        )

    async def assess(
        self,
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
        preferences: str = "",
        lessons: str = "",
    ) -> Assessment:
        user = prompts.assess_user(
            text=text,
            language=language,
            now=now,
            timezone=timezone,
            projects=projects,
            candidates=candidates,
            history=history,
            pending_questions=pending_questions,
            original_text=original_text,
            user_preferences=preferences,
            lessons=lessons,
        )
        assessment = await self._structured(
            Assessment, system=self.prompts.assess_system(), user=user
        )
        if not assessment.language:
            assessment.language = language
        return assessment

    async def answer(
        self,
        *,
        question: str,
        notes: Sequence[Note],
        conflicts: Sequence[dict[str, object]],
        history: Sequence[dict[str, str]] | None = None,
        lessons: str = "",
        preferences: str = "",
        detail: str = "normal",
        language: str = "en",
    ) -> AnswerDraft:
        user = prompts.answer_user(
            question=question,
            notes=notes,
            conflicts=conflicts,
            history=history,
            lessons=lessons,
            preferences=preferences,
            detail=detail,
            language=language,
        )
        return await self._structured(AnswerDraft, system=self.prompts.answer_system(), user=user)

    async def reflect(self, *, transcript: str) -> ReflectionDraft:
        return await self._structured(
            ReflectionDraft,
            system=self.prompts.reflection_system(),
            user=prompts.reflection_user(transcript=transcript),
        )

    async def inventory(self, *, items: str, stats: str) -> InventoryDraft:
        return await self._structured(
            InventoryDraft,
            system=self.prompts.inventory_system(),
            user=prompts.inventory_user(items=items, stats=stats),
            max_tokens=3000,
        )

    async def proactive(self, *, now: str, candidates: str, recent: str, last_sent: str | None) -> ProactiveDraft:
        return await self._structured(
            ProactiveDraft,
            system=self.prompts.proactive_system(),
            user=prompts.proactive_user(now=now, candidates=candidates, recent=recent, last_sent=last_sent),
            max_tokens=900,
        )


def safe_llm_error(exc: Exception) -> str:
    """Turn a provider failure into a short, honest sentence."""

    if isinstance(exc, LLMError):
        return f"The reasoning model failed: {exc}"
    return f"Reasoning failed: {type(exc).__name__}: {exc}"
