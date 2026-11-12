"""Curated notes: rendering, versioning, privacy, and file durability."""

from __future__ import annotations

from alan.models import NoteContent
from alan.store import notes as notes_store
from alan.store import raw


def _content(**overrides) -> NoteContent:
    base = NoteContent(
        title="Backend vs AI learning priorities",
        type="decision",
        status="active",
        summary="Keep backend primary; add AI in parallel.",
        decision="Backend remains primary.",
        reasoning="Backend is active and work capacity is limited.",
        context="Learning plan discussion.",
        constraints=["Study time is uncertain."],
        assumptions=["Coursework continues."],
        alternatives=["Replace backend with AI."],
        open_questions=["How many hours per week?"],
        next_actions=["Draft a schedule."],
        deadlines=["2026-12-01T00:00:00+00:00"],
        tags=["learning", "priorities"],
        language="ru",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_create_writes_markdown_and_db(cfg, db):
    capture = raw.create(db, cfg, chat_id=1, text="Бэкенд остаётся основным.")
    note = notes_store.create(db, cfg, _content(), raw_ids=[capture.id])

    assert note.version == 1
    assert note.slug == "backend-vs-ai-learning-priorities"
    path = cfg.note_path(note.path)
    assert path.exists()

    body = path.read_text(encoding="utf-8")
    assert "title: Backend vs AI learning priorities" in body
    assert "## Decision" in body
    assert "## Constraints" in body
    assert "## Open questions" in body
    assert capture.path in body
    assert "Бэкенд остаётся основным." in body

    assert notes_store.get(db, note.id).tags == ["learning", "priorities"]


def test_update_creates_a_version_and_keeps_history(cfg, db):
    capture = raw.create(db, cfg, chat_id=1, text="initial")
    note = notes_store.create(db, cfg, _content(), raw_ids=[capture.id])

    updated = notes_store.update_content(
        db, cfg, note.id,
        _content(status="paused", summary="Paused until January."),
        raw_ids=[],
        reason="User paused the plan.",
    )
    assert updated.version == 2

    versions = notes_store.versions(db, note.id)
    assert [v["version"] for v in versions] == [2, 1]
    original = notes_store.version_body(db, note.id, 1)
    assert "Keep backend primary; add AI in parallel." in original


def test_slug_collisions_are_resolved(cfg, db):
    capture = raw.create(db, cfg, chat_id=1, text="x")
    first = notes_store.create(db, cfg, _content(), raw_ids=[capture.id])
    second = notes_store.create(db, cfg, _content(), raw_ids=[capture.id])
    assert first.slug != second.slug
    assert second.slug.endswith("-2")


def test_private_notes_leave_the_index(cfg, db):
    capture = raw.create(db, cfg, chat_id=1, text="secret")
    note = notes_store.create(db, cfg, _content(title="Private matter"), raw_ids=[capture.id])
    assert db.scalar("SELECT COUNT(*) FROM notes_fts WHERE note_id = ?", (note.id,)) == 1

    notes_store.set_private(db, note.id, True)
    assert db.scalar("SELECT COUNT(*) FROM notes_fts WHERE note_id = ?", (note.id,)) == 0

    notes_store.set_private(db, note.id, False)
    assert db.scalar("SELECT COUNT(*) FROM notes_fts WHERE note_id = ?", (note.id,)) == 1


def test_delete_removes_file_and_row_but_not_raw(cfg, db):
    capture = raw.create(db, cfg, chat_id=1, text="keep me")
    note = notes_store.create(db, cfg, _content(), raw_ids=[capture.id])
    path = cfg.note_path(note.path)

    assert notes_store.delete(db, cfg, note.id) is True
    assert not path.exists()
    assert notes_store.get(db, note.id) is None
    assert raw.get(db, capture.id) is not None


def test_stats_are_consistent(cfg, db):
    capture = raw.create(db, cfg, chat_id=1, text="x")
    notes_store.create(db, cfg, _content(), raw_ids=[capture.id])
    stats = notes_store.stats(db)
    assert stats["notes"] == 1
    assert stats["raw"] == 1
    assert stats["active"] == 1

