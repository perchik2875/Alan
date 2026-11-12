from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import pytest
import pytest_asyncio

from alan.config import Config, EmbeddingConfig, LLMConfig, ProactiveConfig
from alan.core.service import AlanService
from alan.db import Database
from alan.llm.base import LLMResponse, Message


class FakeLLM:
    provider = "fake"
    model = "fake-1"

    def __init__(self, responses: Sequence[str | dict] | None = None) -> None:
        self.responses: list[str] = []
        for item in responses or []:
            self.responses.append(item if isinstance(item, str) else json.dumps(item))
        self.calls: list[dict] = []
        self.closed = False

    def queue(self, item: str | dict) -> None:
        self.responses.append(item if isinstance(item, str) else json.dumps(item))

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        self.calls.append(
            {
                "system": system,
                "messages": [(m.role, m.content) for m in messages],
                "json_mode": json_mode,
            }
        )
        if not self.responses:
            raise AssertionError("FakeLLM ran out of queued responses")
        text = self.responses.pop(0)
        return LLMResponse(text=text, model=self.model, provider=self.provider)

    async def aclose(self) -> None:
        self.closed = True


class BagEmbedder:
    """Deterministic character-trigram vectors: good enough to test fusion."""

    enabled = True
    model = "fake-embed"

    def __init__(self, dims: int = 64) -> None:
        self.dims = dims

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dims
        for word in text.lower().split():
            key = sum(ord(ch) for ch in word)
            vector[key % self.dims] += 1.0
        return vector

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    async def aclose(self) -> None:
        return None


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    data = tmp_path / "data"
    config = Config(
        telegram_token="test",
        data_dir=data,
        db_path=data / "alan.db",
        llm=LLMConfig(provider="openai", model="fake", api_key="k", max_retries=0),
        embeddings=EmbeddingConfig(provider="none"),
        proactive=ProactiveConfig(enabled=True, quiet_start=0, quiet_end=0, min_hours_between=0),
        onboarding_enabled=False,
    )
    config.ensure_dirs()
    return config


@pytest.fixture
def db(cfg: Config) -> Database:
    database = Database(cfg.db_path)
    yield database
    database.close()


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def embedder() -> BagEmbedder:
    return BagEmbedder()


@pytest.fixture
def service(cfg: Config, db: Database, llm: FakeLLM) -> AlanService:
    return AlanService(cfg=cfg, db=db, llm=llm, embedder=BagEmbedder())


# --------------------------------------------------------------------------
# Assessment payload builders
# --------------------------------------------------------------------------

def capture(
    *,
    title: str = "Backend vs AI learning priorities",
    type: str = "decision",
    summary: str = "Keep backend as the primary track; add AI as a parallel track.",
    project: str | None = "Learning plan",
    project_is_new: bool = False,
    updates_note_id: str | None = None,
    links: list[dict] | None = None,
    conflicts: list[dict] | None = None,
    language: str = "en",
    tags: list[str] | None = None,
    open_questions: list[str] | None = None,
    sensitive: bool = False,
) -> dict:
    return {
        "intent": "capture",
        "language": language,
        "needs_clarification": False,
        "clarifying_questions": [],
        "assumptions": [],
        "search_query": "",
        "confidence": 0.9,
        "note": {
            "title": title,
            "type": type,
            "status": "active",
            "summary": summary,
            "decision": summary if type == "decision" else "",
            "reasoning": "Backend is already an active direction. AI may offer higher long-term growth.",
            "context": "Discussion of the learning plan alongside work.",
            "constraints": ["Available study time is uncertain."],
            "assumptions": [],
            "alternatives": ["Replace backend with AI entirely."],
            "open_questions": open_questions or [],
            "next_actions": ["Draft a weekly schedule."],
            "deadlines": [],
            "tags": tags or ["learning", "priorities"],
            "project": project,
            "project_is_new": project_is_new,
            "sensitive": sensitive,
            "updates_note_id": updates_note_id,
        },
        "links": links or [],
        "conflicts": conflicts or [],
        "correction": None,
    }


def question(search_query: str = "learning plan priorities") -> dict:
    return {
        "intent": "question",
        "language": "en",
        "needs_clarification": False,
        "note": None,
        "search_query": search_query,
        "links": [],
        "conflicts": [],
    }


def answer(text: str, sources: list[str] | None = None) -> dict:
    return {
        "answer": text,
        "sources": sources or [],
        "evidence": [{"statement": text, "basis": "fact", "note_ids": sources or []}],
        "unresolved": [],
    }
