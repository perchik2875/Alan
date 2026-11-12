"""Retrieval: exact-term (FTS5) and meaning-based (vectors), fused.

Neither retriever is trusted on its own.  SQLite FTS5 handles names, numbers,
and exact phrases; embeddings handle paraphrase and cross-language recall.
Reciprocal Rank Fusion combines them without needing comparable score scales.
"""

from __future__ import annotations

import math
import re
from array import array
from typing import Iterable

from ..db import Database
from ..models import Note, SearchHit
from ..textutil import one_line
from .. import rus
from . import notes as notes_store

RRF_K = 60
# note_id, title, summary, tags, project, body, roots
FTS_WEIGHTS = "0.0, 8.0, 5.0, 4.0, 3.0, 1.0, 0.5"

_TOKEN_RE = re.compile(r"[\w\u0400-\u04FF]+", re.UNICODE)


# --------------------------------------------------------------------------
# FTS
# --------------------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text or "") if t.strip()]


def build_match_query(text: str, *, mode: str = "or") -> str:
    tokens = []
    for token in rus.query_terms(text):
        escaped = token.replace('"', '""')
        if not escaped:
            continue
        tokens.append(f'"{escaped}"*')
    if not tokens:
        return ""
    joiner = " AND " if mode == "and" else " OR "
    return joiner.join(tokens)


def fts_upsert(db: Database, note: Note) -> None:
    fts_delete(db, note.id)
    if not note.indexed or note.sensitive:
        return
    project = db.query_one("SELECT name FROM projects WHERE id = ?", (note.project_id,)) if note.project_id else None
    project_name = project["name"] if project else ""
    stems, roots = rus.index_text(
        "\n".join(
            (
                note.title,
                note.summary,
                " ".join(note.tags),
                project_name,
                note.body_md,
            )
        )
    )
    db.execute(
        "INSERT INTO notes_fts(note_id, title, summary, tags, project, body, roots) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            note.id,
            rus.index_text(note.title)[0],
            rus.index_text(note.summary)[0],
            rus.index_text(" ".join(note.tags))[0],
            rus.index_text(project_name)[0],
            stems,
            roots,
        ),
    )


def fts_delete(db: Database, note_id: str) -> None:
    db.execute("DELETE FROM notes_fts WHERE note_id = ?", (note_id,))


def fts_search(db: Database, query: str, *, limit: int = 40) -> list[tuple[str, float]]:
    match = build_match_query(query)
    if not match:
        return []
    try:
        rows = db.query(
            f"SELECT note_id, bm25(notes_fts, {FTS_WEIGHTS}) AS rank "
            "FROM notes_fts WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        )
    except Exception:
        # A malformed MATCH expression must never break retrieval.  The
        # fallback stays inside the public index, so private notes remain
        # invisible.
        like = f"%{query.strip()}%"
        rows = db.query(
            "SELECT id AS note_id, 0.0 AS rank FROM notes "
            "WHERE indexed = 1 AND sensitive = 0 AND (title LIKE ? OR summary LIKE ?) "
            "ORDER BY updated_at DESC LIMIT ?",
            (like, like, limit),
        )
    return [(row["note_id"], float(row["rank"] or 0.0)) for row in rows]


def fts_rebuild(db: Database) -> int:
    db.execute("DELETE FROM notes_fts")
    count = 0
    for note in notes_store.list_notes(db, limit=100000, include_sensitive=True):
        fts_upsert(db, note)
        count += 1
    return count


# --------------------------------------------------------------------------
# Vectors
# --------------------------------------------------------------------------

def pack(vector: Iterable[float]) -> bytes:
    arr = array("f", [float(x) for x in vector])
    return arr.tobytes()


def unpack(blob: bytes) -> array:
    arr = array("f")
    arr.frombytes(blob)
    return arr


def cosine(a: Iterable[float], b: Iterable[float]) -> float:
    a = list(a)
    b = list(b)
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def save_embedding(db: Database, note_id: str, model: str, vector: list[float], *, at: str) -> None:
    db.execute(
        "INSERT INTO embeddings(note_id, model, dim, vector, updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(note_id, model) DO UPDATE SET dim = excluded.dim, "
        "vector = excluded.vector, updated_at = excluded.updated_at",
        (note_id, model, len(vector), pack(vector), at),
    )


def delete_embedding(db: Database, note_id: str) -> None:
    db.execute("DELETE FROM embeddings WHERE note_id = ?", (note_id,))


def vector_search(
    db: Database,
    query_vector: list[float],
    model: str,
    *,
    limit: int = 40,
    min_score: float = 0.15,
) -> list[tuple[str, float]]:
    rows = db.query(
        "SELECT e.note_id, e.vector FROM embeddings e JOIN notes n ON n.id = e.note_id "
        "WHERE e.model = ? AND n.indexed = 1 AND n.sensitive = 0",
        (model,),
    )
    scored: list[tuple[str, float]] = []
    for row in rows:
        score = cosine(query_vector, unpack(row["vector"]))
        if score >= min_score:
            scored.append((row["note_id"], score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:limit]


def embedding_text(note: Note) -> str:
    content = note.content or {}
    parts = [note.title, note.summary, " ".join(note.tags)]
    for key in ("decision", "reasoning", "context"):
        value = content.get(key)
        if value:
            parts.append(str(value))
    for key in ("open_questions", "next_actions", "constraints", "alternatives"):
        values = content.get(key) or []
        if values:
            parts.append(" ".join(str(v) for v in values))
    text = "\n".join(p for p in parts if p)
    return text[:6000]


def missing_embeddings(db: Database, model: str, *, limit: int = 200) -> list[Note]:
    rows = db.query(
        "SELECT n.id FROM notes n LEFT JOIN embeddings e "
        "ON e.note_id = n.id AND e.model = ? "
        "WHERE e.note_id IS NULL AND n.indexed = 1 AND n.sensitive = 0 LIMIT ?",
        (model, limit),
    )
    return [n for n in (notes_store.get(db, row["id"]) for row in rows) if n is not None]


# --------------------------------------------------------------------------
# Hybrid
# --------------------------------------------------------------------------

def hybrid_search(
    db: Database,
    query: str,
    *,
    query_vector: list[float] | None = None,
    embedding_model: str | None = None,
    top_k: int = 6,
    candidate_limit: int = 40,
    include_sensitive: bool = False,
    since: str | None = None,
) -> list[SearchHit]:
    if not query.strip():
        return []

    fts_hits = fts_search(db, query, limit=candidate_limit)
    vec_hits: list[tuple[str, float]] = []
    if query_vector and embedding_model:
        vec_hits = vector_search(db, query_vector, embedding_model, limit=candidate_limit)

    fused: dict[str, dict[str, object]] = {}
    for rank, (note_id, score) in enumerate(fts_hits):
        entry = fused.setdefault(note_id, {"score": 0.0, "methods": [], "fts": 0.0, "vec": 0.0})
        entry["score"] = float(entry["score"]) + 1.0 / (RRF_K + rank + 1)
        entry["methods"].append("exact")
        entry["fts"] = score
    for rank, (note_id, score) in enumerate(vec_hits):
        entry = fused.setdefault(note_id, {"score": 0.0, "methods": [], "fts": 0.0, "vec": 0.0})
        entry["score"] = float(entry["score"]) + 1.0 / (RRF_K + rank + 1)
        entry["methods"].append("meaning")
        entry["vec"] = score

    hits: list[SearchHit] = []
    for note_id, entry in fused.items():
        note = notes_store.get(db, note_id)
        if note is None:
            continue
        if note.sensitive and not include_sensitive:
            continue
        if since and note.updated_at < since:
            continue
        hits.append(
            SearchHit(
                note=note,
                score=float(entry["score"]),
                methods=tuple(dict.fromkeys(entry["methods"])),
                fts_rank=float(entry["fts"]),
                vector_score=float(entry["vec"]),
            )
        )

    hits.sort(key=lambda hit: hit.score, reverse=True)

    if include_sensitive and len(hits) < top_k and query.strip():
        seen = {hit.note.id for hit in hits}
        like = f"%{query.strip()}%"
        for row in db.query(
            "SELECT * FROM notes WHERE sensitive = 1 AND (title LIKE ? OR summary LIKE ?) "
            "ORDER BY updated_at DESC LIMIT ?",
            (like, like, top_k),
        ):
            if row["id"] in seen:
                continue
            note = notes_store.get(db, row["id"])
            if note:
                hits.append(SearchHit(note=note, score=0.0, methods=("private",)))

    return hits[:top_k]


def exact_term_hits(
    db: Database, needle: str, *, limit: int = 10, include_sensitive: bool = False
) -> list[Note]:
    """Case-insensitive substring lookup across titles, summaries and bodies.

    Privacy is applied in SQL, so this cannot become a back door around the
    sensitive-note filter.
    """

    needle = one_line(needle)
    if not needle:
        return []
    privacy = "" if include_sensitive else " AND sensitive = 0"
    rows = db.query(
        "SELECT id FROM notes WHERE (title LIKE ? OR summary LIKE ? OR body_md LIKE ?)" + privacy +
        " ORDER BY updated_at DESC LIMIT ?",
        (f"%{needle}%", f"%{needle}%", f"%{needle}%", limit),
    )
    return [n for n in (notes_store.get(db, row["id"]) for row in rows) if n is not None]
