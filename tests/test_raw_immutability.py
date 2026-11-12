"""The central promise: nothing the user said can be silently changed."""

from __future__ import annotations

import sqlite3

import pytest

from alan.store import audit, raw


def test_raw_capture_roundtrip_preserves_text_exactly(cfg, db):
    text = "  Мысль: бэкенд остаётся основным.\n\nAI — параллельно.  \n"
    capture = raw.create(db, cfg, chat_id=1, text=text, message_id=10)

    stored = raw.get(db, capture.id)
    assert stored is not None
    assert stored.text == text
    assert stored.language == "ru"
    assert stored.content_hash

    path = cfg.data_dir / capture.path
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert text in content


def test_update_is_rejected_by_the_database(cfg, db):
    capture = raw.create(db, cfg, chat_id=1, text="original")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE raw_captures SET text = 'changed' WHERE id = ?", (capture.id,))

    assert raw.get(db, capture.id).text == "original"


def test_delete_requires_explicit_user_action(cfg, db):
    capture = raw.create(db, cfg, chat_id=1, text="original")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM raw_captures WHERE id = ?", (capture.id,))

    assert raw.delete(db, cfg, capture.id, actor="user") is True
    assert raw.get(db, capture.id) is None

    actions = [row["action"] for row in audit.recent(db)]
    assert "raw_capture.delete.requested" in actions
    assert "raw_capture.delete.done" in actions


def test_python_level_update_guard(cfg, db):
    from alan.errors import ImmutableRecord

    with pytest.raises(ImmutableRecord):
        raw.update("anything")


def test_many_captures_are_independent(cfg, db):
    first = raw.create(db, cfg, chat_id=1, text="one")
    second = raw.create(db, cfg, chat_id=1, text="two")
    assert first.id != second.id
    assert first.path != second.path
    assert raw.count(db) == 2
