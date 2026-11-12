"""Retrieval: exact terms, meaning, and fusion."""

from __future__ import annotations

import pytest

from alan.models import NoteContent
from alan.store import notes as notes_store
from alan.store import raw, search
from alan.textutil import now_iso


def _seed(cfg, db, embedder):
    notes = [
        NoteContent(
            title="Backend vs AI learning priorities",
            type="decision", status="active", language="ru",
            summary="Бэкенд остаётся основным, AI — параллельно.",
            tags=["learning"],
        ),
        NoteContent(
            title="Bybit trading bot risk limits",
            type="decision", status="active", language="en",
            summary="Cap position size at 2 percent of equity.",
            tags=["trading"],
        ),
        NoteContent(
            title="Obsidian vault structure",
            type="observation", status="paused", language="en",
            summary="Folders mirror projects, not tags.",
            tags=["tools"],
        ),
    ]
    stored = []
    for content in notes:
        capture = raw.create(db, cfg, chat_id=1, text=content.summary)
        stored.append(notes_store.create(db, cfg, content, raw_ids=[capture.id]))
    return stored


@pytest.mark.asyncio
async def test_exact_term_search_finds_specific_note(cfg, db, embedder):
    stored = _seed(cfg, db, embedder)
    hits = search.fts_search(db, "bybit")
    assert hits
    assert hits[0][0] == stored[1].id


@pytest.mark.asyncio
async def test_fts_prefix_matches_inflected_russian(cfg, db, embedder):
    _seed(cfg, db, embedder)
    hits = search.fts_search(db, "бэкен")
    assert hits


@pytest.mark.asyncio
async def test_vector_search_ranks_related_note(cfg, db, embedder):
    stored = _seed(cfg, db, embedder)
    vectors = await embedder.embed([search.embedding_text(stored[1])])
    for note in stored:
        note_vectors = await embedder.embed([search.embedding_text(note)])
        search.save_embedding(db, note.id, embedder.model, note_vectors[0], at=now_iso())

    hits = search.vector_search(db, vectors[0], embedder.model, min_score=0.0)
    assert hits[0][0] == stored[1].id


@pytest.mark.asyncio
async def test_hybrid_returns_meaning_and_exact_matches(cfg, db, embedder):
    stored = _seed(cfg, db, embedder)
    for note in stored:
        vectors = await embedder.embed([search.embedding_text(note)])
        search.save_embedding(db, note.id, embedder.model, vectors[0], at=now_iso())

    query = "priorities for learning backend"
    query_vector = (await embedder.embed([query]))[0]
    hits = search.hybrid_search(
        db, query, query_vector=query_vector, embedding_model=embedder.model, top_k=3
    )
    assert hits
    assert any("learning" in hit.note.title.lower() for hit in hits)


def test_private_notes_are_excluded_from_search(cfg, db):
    capture = raw.create(db, cfg, chat_id=1, text="x")
    note = notes_store.create(
        db, cfg,
        NoteContent(title="Secret plan", type="fact", status="active", summary="Quietly retire."),
        raw_ids=[capture.id],
    )
    notes_store.set_private(db, note.id, True)
    assert search.fts_search(db, "secret") == []
    assert search.hybrid_search(db, "secret plan") == []


def test_empty_query_is_safe(cfg, db):
    assert search.fts_search(db, "") == []
    assert search.hybrid_search(db, "   ") == []


def test_fts_rebuild_restores_index(cfg, db):
    _seed(cfg, db, None)
    db.execute("DELETE FROM notes_fts")
    assert search.fts_search(db, "bybit") == []
    count = search.fts_rebuild(db)
    assert count == 3
    assert search.fts_search(db, "bybit")
