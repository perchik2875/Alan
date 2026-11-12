"""Proactive behaviour: quiet by default, explainable when it speaks."""

from __future__ import annotations

from datetime import timedelta

import pytest

from alan.models import NoteContent
from alan.proactive import review
from alan.store import notes as notes_store
from alan.store import projects as projects_store
from alan.store import raw, reminders
from alan.textutil import to_iso, utcnow

from conftest import capture


def _old(ts_days: int) -> str:
    return to_iso(utcnow() - timedelta(days=ts_days))


def _plant(db, cfg, *, project: str, days_old: int, title: str):
    p = projects_store.create(db, project)
    cap = raw.create(db, cfg, chat_id=1, text=title)
    note = notes_store.create(
        db, cfg,
        NoteContent(title=title, type="plan", status="active", summary=title),
        raw_ids=[cap.id],
        project_id=p.id,
    )
    db.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (_old(days_old), note.id))
    return note


def test_stale_project_becomes_a_candidate(cfg, db):
    _plant(db, cfg, project="Backend roadmap", days_old=20, title="Roadmap step one")
    _plant(db, cfg, project="Backend roadmap", days_old=19, title="Roadmap step two")

    candidates = review.collect_candidates(db, cfg, 1)
    kinds = [c.kind for c in candidates]
    assert "stale_project" in kinds
    assert any("Backend roadmap" in c.title for c in candidates)


def test_recent_material_is_not_a_candidate(cfg, db):
    _plant(db, cfg, project="Fresh project", days_old=0, title="Fresh note one")
    _plant(db, cfg, project="Fresh project", days_old=0, title="Fresh note two")
    assert review.collect_candidates(db, cfg, 1) == []


def test_open_question_becomes_a_candidate(cfg, db):
    cap = raw.create(db, cfg, chat_id=1, text="Which AI topic first?")
    note = notes_store.create(
        db, cfg,
        NoteContent(title="Which AI topic first?", type="question", status="unresolved"),
        raw_ids=[cap.id],
    )
    db.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (_old(15), note.id))
    candidates = review.collect_candidates(db, cfg, 1)
    assert any(c.kind == "open_question" for c in candidates)


def test_send_window_enforces_daily_cap(cfg, db):
    assert review.within_send_window(db, cfg, 1) is True
    reminders.log_proactive(db, chat_id=1, kinds=["stale_project"], note_ids=["n_x"])
    assert review.within_send_window(db, cfg, 1) is False


def test_quiet_hours_block_sending(cfg, db):
    from dataclasses import replace

    cfg.proactive = replace(cfg.proactive, quiet_start=0, quiet_end=24)
    assert review.within_send_window(db, cfg, 1) is False


@pytest.mark.asyncio
async def test_proactive_check_sends_when_the_model_agrees(service, db, llm, cfg):
    _plant(db, cfg, project="Backend roadmap", days_old=20, title="Roadmap step one")
    _plant(db, cfg, project="Backend roadmap", days_old=19, title="Roadmap step two")

    llm.queue(
        {
            "worth_sending": True,
            "message": "Sir, the backend roadmap has not been updated for three weeks. Shall we review its open decisions?",
            "note_ids": [],
            "kinds": ["stale_project"],
        }
    )
    result = await service.proactive_check(1)
    assert result is not None
    assert "backend roadmap" in result.text.lower()

    # A second check in the same window stays quiet.
    assert await service.proactive_check(1) is None


@pytest.mark.asyncio
async def test_proactive_check_respects_model_refusal(service, db, llm, cfg):
    _plant(db, cfg, project="Backend roadmap", days_old=20, title="Roadmap step one")
    _plant(db, cfg, project="Backend roadmap", days_old=19, title="Roadmap step two")

    llm.queue({"worth_sending": False, "message": "", "note_ids": [], "kinds": []})
    assert await service.proactive_check(1) is None
    assert reminders.last_proactive_at(db, 1) is None


def test_sensitive_notes_are_never_proactive_candidates(cfg, db):
    from alan.models import NoteContent
    from alan.store import notes as notes_store
    from alan.store import projects as projects_store
    from alan.store import raw
    from alan.textutil import to_iso, utcnow
    from datetime import timedelta
    from alan.proactive import review

    project = projects_store.create(db, "Private health")
    for index in range(2):
        capture = raw.create(db, cfg, chat_id=1, text=f"private {index}")
        note = notes_store.create(
            db, cfg,
            NoteContent(title=f"Private note {index}", type="fact", status="active",
                        summary="Sensitive", sensitive=True),
            raw_ids=[capture.id],
            project_id=project.id,
        )
        db.execute(
            "UPDATE notes SET updated_at = ? WHERE id = ?",
            (to_iso(utcnow() - timedelta(days=40)), note.id),
        )

    assert review.collect_candidates(db, cfg, 1) == []
