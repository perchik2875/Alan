"""End-to-end turns through the service, with a scripted model."""

from __future__ import annotations

import pytest

from alan.store import conflicts, lessons, links, messages, notes as notes_store, raw
from conftest import answer, capture, question


@pytest.mark.asyncio
async def test_capture_creates_note_and_preserves_raw(service, db, llm, cfg):
    llm.queue(capture())

    result = await service.handle_text(chat_id=42, text="Бэкенд остаётся основным, AI параллельно.")

    assert not result.error
    # The user wrote in Russian, so the confirmation is in Russian even though
    # the model's note text is English.
    assert "Записано" in result.text
    assert "Решение: Keep backend" in result.text
    assert "Learning plan" in result.text
    assert result.note_id

    note = notes_store.get(db, result.note_id)
    assert note is not None
    assert note.title == "Backend vs AI learning priorities"
    assert note.type == "decision"
    assert note.summary.startswith("Keep backend")
    assert raw.count(db) == 1
    assert (cfg.note_path(note.path)).exists()

    # The project was reused or created, and its name is attached to the note.
    assert note.project_name == "Learning plan"


@pytest.mark.asyncio
async def test_follow_up_updates_the_same_note(service, db, llm):
    llm.queue(capture())
    first = await service.handle_text(chat_id=42, text="Бэкенд остаётся основным.")

    llm.queue(
        capture(
            title="Backend vs AI learning priorities",
            summary="Paused the learning plan until January.",
            updates_note_id=first.note_id,
            project=None,
        )
    )
    second = await service.handle_text(chat_id=42, text="Ставлю учебный план на паузу до января.")

    assert second.note_id == first.note_id
    assert "Обновлено" in second.text
    note = notes_store.get(db, first.note_id)
    assert note.version == 2
    assert "Paused" in note.summary
    # The union of constraints is preserved rather than replaced.
    assert "Available study time is uncertain." in note.content["constraints"]


@pytest.mark.asyncio
async def test_clarification_then_answer_creates_one_note_with_two_sources(service, db, llm):
    llm.queue(
        {
            "intent": "capture",
            "language": "en",
            "needs_clarification": True,
            "clarifying_questions": [
                "Should backend remain the primary track, or should AI replace it?",
                "Is this a decision for the current plan or a new long-term direction?",
            ],
            "note": None,
            "links": [],
            "conflicts": [],
        }
    )
    first = await service.handle_text(
        chat_id=7,
        text="I need to figure out my learning plan. Backend seems more important, but AI might give better growth.",
    )
    assert "clarification" in first.text
    assert messages.get_pending(db, 7)["kind"] == "clarify"
    assert raw.count(db) == 1
    assert notes_store.stats(db)["notes"] == 0

    llm.queue(capture(language="en", project="Learning plan"))
    second = await service.handle_text(
        chat_id=7,
        text="Backend stays primary, AI parallel. For the current plan.",
    )

    assert second.note_id
    assert raw.count(db) == 2
    assert len(raw.for_note(db, second.note_id)) == 2
    assert messages.get_pending(db, 7) is None


@pytest.mark.asyncio
async def test_question_answers_with_sources(service, db, llm):
    llm.queue(capture(language="en", project="Learning plan"))
    created = await service.handle_text(chat_id=1, text="Backend stays primary, AI parallel.")

    llm.queue(question())
    llm.queue(answer("Backend remains primary, with AI in parallel.", [created.note_id]))
    result = await service.handle_text(chat_id=1, text="What did I decide about my learning plan?")

    assert "Backend remains primary" in result.text
    assert "Sources:" in result.text
    assert created.note_id in [n.id for n in notes_store.list_notes(db, limit=5)]


@pytest.mark.asyncio
async def test_correction_reclassifies_and_records_a_lesson(service, db, llm):
    llm.queue(capture(type="decision", language="en", project=None))
    created = await service.handle_text(chat_id=1, text="AI should replace backend.")

    llm.queue(
        {
            "intent": "correction",
            "language": "en",
            "needs_clarification": False,
            "note": None,
            "links": [],
            "conflicts": [],
            "correction": {
                "note_id": created.note_id,
                "changes": {"type": "idea", "status": "unresolved"},
                "lesson": "Do not treat exploratory statements as decisions unless the user confirms them.",
                "lesson_kind": "classification",
            },
        }
    )
    result = await service.handle_text(chat_id=1, text="No, that was not a decision. It was just an idea.")

    assert "Updated" in result.text
    note = notes_store.get(db, created.note_id)
    assert note.type == "idea"
    assert note.version == 2
    assert lessons.count_active(db) == 1
    assert "exploratory" in lessons.list_active(db)[0].text


@pytest.mark.asyncio
async def test_link_proposal_is_confirmed_by_the_user(service, db, llm):
    llm.queue(capture(language="en", project="Learning plan"))
    first = await service.handle_text(chat_id=1, text="Backend stays primary.")

    llm.queue(
        capture(
            title="Weekly study schedule",
            type="plan",
            language="en",
            project="Learning plan",
            links=[{"note_id": first.note_id, "reason": "Implements the learning decision.", "confidence": 0.5}],
        )
    )
    second = await service.handle_text(chat_id=1, text="Draft a weekly study schedule.")

    assert second.buttons, "a low-confidence link should be offered, not applied"
    proposed = links.proposed(db)
    assert len(proposed) == 1
    link_id = proposed[0]["id"]

    result = service.confirm_link(link_id)
    assert "Linked" in result.text
    assert links.outgoing(db, second.note_id, status="confirmed")


@pytest.mark.asyncio
async def test_conflict_is_recorded_and_visible(service, db, llm):
    llm.queue(capture(language="en", project=None, title="Backend priority"))
    first = await service.handle_text(chat_id=1, text="Backend is the primary priority.")

    llm.queue(
        capture(
            title="AI priority",
            language="en",
            project=None,
            conflicts=[
                {
                    "note_id": first.note_id,
                    "existing_claim": "Backend is the primary priority.",
                    "new_claim": "AI should become the primary priority.",
                    "explanation": "Both cannot be the single primary priority.",
                }
            ],
        )
    )
    second = await service.handle_text(chat_id=1, text="AI should become the primary priority.")

    assert "Conflict" in second.text
    rows = conflicts.unresolved(db)
    assert len(rows) == 1
    # Neither note was silently superseded.
    assert notes_store.get(db, first.note_id).status == "active"
    assert notes_store.get(db, second.note_id).status == "active"

    service.resolve_conflict(rows[0]["id"], "new")
    old = notes_store.get(db, first.note_id)
    assert old.status == "superseded"
    assert old.superseded_by == second.note_id


@pytest.mark.asyncio
async def test_feedback_creates_a_lesson(service, db, llm):
    llm.queue(
        {
            "intent": "feedback",
            "language": "en",
            "needs_clarification": False,
            "note": None,
            "links": [],
            "conflicts": [],
            "correction": {
                "note_id": None,
                "changes": {},
                "lesson": "Keep answers to two sentences unless asked for detail.",
                "lesson_kind": "tone",
            },
        }
    )
    result = await service.handle_text(chat_id=1, text="Your answers are too long. Be briefer.")
    assert "Noted" in result.text
    assert lessons.count_active(db) == 1


@pytest.mark.asyncio
async def test_private_note_is_not_returned_in_answers(service, db, llm):
    llm.queue(capture(language="en", title="Bank credentials", project=None, sensitive=True))
    created = await service.handle_text(chat_id=1, text="My bank password is hunter2.")

    note = notes_store.get(db, created.note_id)
    assert note.sensitive is True
    assert db.scalar("SELECT COUNT(*) FROM notes_fts WHERE note_id = ?", (note.id,)) == 0


@pytest.mark.asyncio
async def test_model_errors_do_not_lose_the_message(service, db, llm):
    class Broken:
        provider = "broken"
        model = "broken"

        async def complete(self, **kwargs):
            from alan.errors import LLMError

            raise LLMError("provider down")

        async def aclose(self):
            return None

    service.llm = Broken()
    service.reasoner.llm = service.llm

    result = await service.handle_text(chat_id=1, text="Important thought")
    assert result.error
    assert raw.count(db) == 1


@pytest.mark.asyncio
async def test_new_project_is_provisional_until_confirmed(service, db, llm):
    from alan.store import projects as projects_store

    llm.queue(capture(project="Skill roadmap", project_is_new=True))
    result = await service.handle_text(chat_id=1, text="I should build a skill roadmap.")

    project = projects_store.find_by_name(db, "Skill roadmap")
    assert project is not None
    assert project.confirmed is False
    assert "Proposed project" in result.text
    assert any(button.data.startswith("project:confirm:") for row in result.buttons for button in row)

    service.confirm_project(project.id)
    assert projects_store.get(db, project.id).confirmed is True


@pytest.mark.asyncio
async def test_chitchat_gets_a_direct_short_reply(service, db, llm):
    llm.queue(
        {
            "intent": "chitchat",
            "language": "en",
            "note": None,
            "reply": "At your service, sir.",
        }
    )
    result = await service.handle_text(chat_id=1, text="thanks")
    assert result.text == "At your service, sir."
    assert result.note_id is None
    # The message is still preserved.
    assert raw.count(db) == 1


@pytest.mark.asyncio
async def test_correction_with_unknown_target_is_not_lost(service, db, llm):
    llm.queue(
        {
            "intent": "correction",
            "language": "en",
            "note": {
                "title": "Corrected position on AI",
                "type": "decision",
                "status": "active",
                "summary": "AI is a parallel track, not a replacement.",
                "project": None,
            },
            "links": [],
            "conflicts": [],
            "correction": {
                "note_id": "n_missing",
                "changes": {"summary": "AI is a parallel track."},
                "lesson": "",
            },
        }
    )
    result = await service.handle_text(chat_id=1, text="No, that note is wrong: AI is parallel, not a replacement.")

    assert result.note_id
    assert "could not find the record" in result.text.lower()
    assert notes_store.stats(db)["notes"] == 1

