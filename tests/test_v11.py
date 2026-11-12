"""Acceptance tests for Alan v1.1 (spec section 12)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from alan.core.service import AlanService
from alan.db import Database
from alan.models import NoteContent
from alan.store import lessons, messages, notes as notes_store, prefs, raw, search
from conftest import BagEmbedder, answer, capture, question


def _seed_note(cfg, db, *, title="Решение по архитектуре", summary="Решение по архитектуре: модульный монолит.",
               language="ru"):
    return notes_store.create(
        db,
        cfg,
        NoteContent(
            title=title,
            type="decision",
            status="active",
            language=language,
            summary=summary,
        ),
        raw_ids=[],
    )


# --------------------------------------------------------------------------
# 12.1 Conversation continuity
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_conversation_continuity_survives_restart(cfg, db, llm):
    service = AlanService(cfg=cfg, db=db, llm=llm, embedder=BagEmbedder())

    llm.queue(capture(language="ru"))
    first = await service.handle_text(chat_id=901, text="Бэкенд остаётся основным.")
    assert first.note_id

    # The second message refers back to the first; the model sees the history.
    llm.queue(capture(language="ru", title="Бэкенд и AI", summary="AI идёт параллельно.",
                      updates_note_id=first.note_id))
    second = await service.handle_text(chat_id=901, text="Добавь к этому: AI идёт параллельно.")
    assert second.note_id == first.note_id
    assert "Бэкенд остаётся основным." in llm.calls[1]["messages"][-1][1]

    # Restart: a fresh service and a fresh connection to the same database.
    db2 = Database(cfg.db_path)
    try:
        restarted = AlanService(cfg=cfg, db=db2, llm=llm, embedder=BagEmbedder())
        remembered = await restarted.handle_text(chat_id=901, text="Что ты помнишь?")
        assert "Бэкенд остаётся основным." in remembered.text
        previous = await restarted.handle_text(chat_id=901, text="Какое моё предыдущее сообщение?")
        assert "Что ты помнишь" in previous.text or "Добавь" in previous.text
    finally:
        db2.close()


# --------------------------------------------------------------------------
# 12.2 Language consistency
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_language_follows_the_user_and_raw_text_is_untouched(cfg, db, llm):
    service = AlanService(cfg=cfg, db=db, llm=llm, embedder=BagEmbedder())

    llm.queue(capture(language="ru"))
    ru = await service.handle_text(chat_id=902, text="Решение: бэкенд остаётся основным.")
    assert "Записано" in ru.text
    assert "Recorded" not in ru.text

    # A one-word reply carries too little text for detection.  It must stay in
    # the conversation's language rather than falling back to English.
    llm.queue(capture(language="ru", title="Короткое подтверждение"))
    short = await service.handle_text(chat_id=902, text="Да.")
    assert "Записано" in short.text

    llm.queue({"intent": "chitchat", "language": "en", "note": None,
               "reply": "At your service, sir."})
    en = await service.handle_text(chat_id=902, text="Thanks, that helps.")
    assert en.text == "At your service, sir."

    captures = raw.list_recent(db, limit=5, chat_id="902")
    texts = {c.text for c in captures}
    assert "Решение: бэкенд остаётся основным." in texts
    assert "Thanks, that helps." in texts
    assert next(c for c in captures if c.text.startswith("Решение")).language == "ru"

    note = notes_store.get(db, ru.note_id)
    assert note is not None
    assert all(tag.isascii() for tag in note.tags)


# --------------------------------------------------------------------------
# 12.3 Lesson discipline
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_questions_do_not_create_lessons(cfg, db, llm):
    service = AlanService(cfg=cfg, db=db, llm=llm, embedder=BagEmbedder())
    note = _seed_note(cfg, db, title="Backend priority", summary="Backend remains primary.", language="en")

    llm.queue(question("backend priority"))
    llm.queue(answer("Backend remains primary, sir.", [note.id]))
    result = await service.handle_text(chat_id=903, text="Why did you record that as a decision?")
    assert result.text
    assert lessons.count_active(db) == 0


@pytest.mark.asyncio
async def test_ambiguous_preference_is_confirmed_before_becoming_a_lesson(cfg, db, llm):
    service = AlanService(cfg=cfg, db=db, llm=llm, embedder=BagEmbedder())

    llm.queue(
        {
            "intent": "feedback",
            "language": "en",
            "needs_clarification": False,
            "note": None,
            "links": [],
            "conflicts": [],
            "durable_intent": "unclear",
            "lesson": {
                "text": "Keep answers to two sentences unless the user asks for detail.",
                "kind": "tone",
                "scope": "global",
                "confidence": 0.84,
                "explanation": "The user asked for brevity.",
            },
        }
    )
    asked = await service.handle_text(chat_id=904, text="Always answer briefly.")
    assert "one-time" in asked.text.lower() or "lasting" in asked.text.lower()
    pending = messages.get_pending(db, 904)
    assert pending and pending["kind"] == "lesson_confirm"
    assert lessons.count_active(db) == 0

    confirmed = await service.handle_text(chat_id=904, text="Yes, a lasting preference.")
    assert "Recorded as a lesson" in confirmed.text
    assert lessons.count_active(db) == 1
    lesson = lessons.list_active(db)[0]
    assert lesson.confidence == pytest.approx(0.84)
    assert lesson.evidence_raw_id
    assert "two sentences" in lesson.text

    assert lessons.deactivate(db, lesson.id) is True
    assert lessons.count_active(db) == 0


@pytest.mark.asyncio
async def test_one_time_instruction_does_not_become_a_lesson(cfg, db, llm):
    service = AlanService(cfg=cfg, db=db, llm=llm, embedder=BagEmbedder())

    llm.queue(
        {
            "intent": "feedback",
            "language": "en",
            "durable_intent": "one_time",
            "lesson": {"text": "Answer this one in detail.", "confidence": 0.9},
        }
    )
    result = await service.handle_text(chat_id=905, text="Answer this one in detail, please.")
    assert lessons.count_active(db) == 0
    assert "this time" in result.text.lower() or "one-time" in result.text.lower() or "разово" in result.text.lower()


# --------------------------------------------------------------------------
# 12.4 Self-disclosure
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_system_prompt_is_explained_without_leaking_secrets(cfg, db, llm):
    cfg.llm.api_key = "sk-supersecret-123456789"
    service = AlanService(cfg=cfg, db=db, llm=llm, embedder=BagEmbedder())

    result = await service.handle_text(chat_id=906, text="Покажи свой системный промпт.")
    assert ".alan/prompts" in result.text
    assert "sk-supersecret-123456789" not in result.text
    assert "Behaviour" in result.text or "Поведение" in result.text
    assert result.intent == "self_disclosure"
    assert lessons.count_active(db) == 0


# --------------------------------------------------------------------------
# 12.5 Markdown preservation
# --------------------------------------------------------------------------

def test_external_markdown_edit_is_versioned_not_overwritten(cfg, db):
    note = _seed_note(
        cfg,
        db,
        title="Backend priority",
        summary="Keep backend primary; add AI in parallel.",
        language="en",
    )
    path = cfg.note_path(note.path)
    original = path.read_text(encoding="utf-8")
    edited = original.replace(
        "Keep backend primary; add AI in parallel.",
        "Keep backend primary; drop AI for now.",
    )
    assert edited != original
    path.write_text(edited, encoding="utf-8")
    future = time.time() + 10
    os.utime(path, (future, future))

    service = AlanService(cfg=cfg, db=db, llm=object(), embedder=BagEmbedder())
    changed = service.sync_markdown()
    assert note.id in changed

    updated = notes_store.get(db, note.id)
    assert updated.version == 2
    assert "drop AI" in updated.summary
    assert path.read_text(encoding="utf-8") == edited
    assert "add AI in parallel" in notes_store.version_body(db, note.id, 1)

    hits = search.hybrid_search(db, "drop AI")
    assert any(hit.note.id == note.id for hit in hits)


# --------------------------------------------------------------------------
# 12.6 Russian retrieval
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_russian_inflection_retrieves_the_note_and_cites_it(cfg, db, llm):
    note = _seed_note(cfg, db)
    service = AlanService(cfg=cfg, db=db, llm=llm, embedder=BagEmbedder())

    hits = await service.search_notes("Как я решал архитектурный вопрос?")
    assert any(hit.note.id == note.id for hit in hits)

    llm.queue(question("архитектурный вопрос"))
    llm.queue(answer("Вы выбрали модульный монолит.", [note.id]))
    result = await service.handle_text(chat_id=907, text="Как я решал архитектурный вопрос?")
    assert "модульный монолит" in result.text
    assert note.slug in result.text


# --------------------------------------------------------------------------
# 12.7 Import resilience
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_creates_raw_first_and_resumes_after_restart(cfg, db, llm, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    for index in range(8):
        (vault / f"note{index}.md").write_text(
            f"# Entry {index}\n\n" + f"Imported decision number {index}. " * 8,
            encoding="utf-8",
        )

    service = AlanService(cfg=cfg, db=db, llm=llm, embedder=BagEmbedder())
    job = service.start_import(vault, chat_id=908)

    # Stage 1 is complete before any model call: all originals are safe.
    assert job["total"] == 8
    assert raw.count(db) == 8
    assert notes_store.stats(db)["notes"] == 0

    assert service.pause_import(job["id"]) is True
    assert service.import_report(job["id"]).lower().count("status") >= 1

    # Restart with a fresh connection; nothing already captured is lost.
    db2 = Database(cfg.db_path)
    try:
        assert raw.count(db2) == 8
        restarted = AlanService(cfg=cfg, db=db2, llm=llm, embedder=BagEmbedder())
        for _ in range(8):
            llm.queue(capture(title="Imported entry", project=None))
        progress: list[str] = []

        async def report(text: str) -> None:
            progress.append(text)

        result = await restarted.resume_import(job["id"], progress=report)
        assert result.items == 8
        assert result.captures == 8
        assert result.notes == 8
        assert raw.count(db2) == 8
        assert progress, "progress must be observable"
        assert notes_store.stats(db2)["notes"] == 8
    finally:
        db2.close()


@pytest.mark.asyncio
async def test_resume_captures_outstanding_items_before_analysis(cfg, db, llm, tmp_path):
    """A crash during stage 1 must be repaired on resume, before any model call."""

    from types import SimpleNamespace

    from alan.store import imports as imports_store

    service = AlanService(cfg=cfg, db=db, llm=llm, embedder=BagEmbedder())
    job = imports_store.create(db, chat_id=910, source="file-import")
    imports_store.add_items(
        db,
        job["id"],
        [
            SimpleNamespace(text="Первое решение: оставить бэкенд.", received_at="2026-11-01T10:00:00+00:00"),
            SimpleNamespace(text="Второе решение: AI параллельно.", received_at="2026-11-02T10:00:00+00:00"),
        ],
    )
    assert raw.count(db) == 0  # stage 1 never ran

    for _ in range(2):
        llm.queue(capture(title="Импортированная запись", project=None, language="ru"))

    result = await service.process_import(job["id"], chat_id=910)
    assert result.captures == 2
    assert raw.count(db) == 2
    items = imports_store.items(db, job["id"])
    assert all(item["raw_id"] for item in items)
    assert all(item["status"] == "done" for item in items)


# --------------------------------------------------------------------------
# Onboarding (spec section 8)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_onboarding_is_short_and_creates_a_profile(cfg, db, llm):
    cfg.onboarding_enabled = True
    service = AlanService(cfg=cfg, db=db, llm=llm, embedder=BagEmbedder())

    first = service.start_onboarding(909, "ru")
    assert first and "настройк" in first.lower()
    assert messages.get_pending(db, 909)["kind"] == "onboarding"

    await service.handle_text(chat_id=909, text="Обучение и разработка.")
    await service.handle_text(chat_id=909, text="Alan и личный архив знаний.")
    summary = await service.handle_text(chat_id=909, text="Идеи и решения.")

    assert "Сохранить" in summary.text
    assert messages.get_pending(db, 909)["kind"] == "onboarding_confirm"
    assert prefs.get_value(db, "onboarding_done") != "1"
    assert notes_store.find(db, "user-profile") is None

    done = await service.handle_text(chat_id=909, text="Да, сохранить профиль.")

    assert "Готово" in done.text
    assert prefs.get_value(db, "onboarding_done") == "1"
    profile = notes_store.find(db, "user-profile")
    assert profile is not None
    assert "Обучение" in profile.summary or "разработка" in profile.summary

