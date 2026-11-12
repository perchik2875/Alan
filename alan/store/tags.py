"""Tags.

Tags are cheap, reversible metadata.  Unlike projects they do not require
confirmation: a bad tag costs nothing, an unwanted project pollutes the map.
"""

from __future__ import annotations

from ..db import Database
from ..textutil import slugify


def _ensure(db: Database, name: str) -> int | None:
    clean = name.strip().lstrip("#").strip()
    if not clean:
        return None
    clean = clean[:48]
    slug = slugify(clean, fallback="tag", max_len=48)
    row = db.query_one("SELECT id FROM tags WHERE slug = ? OR name = ?", (slug, clean))
    if row:
        return int(row["id"])
    cur = db.execute("INSERT INTO tags(name, slug) VALUES(?,?)", (clean, slug))
    return int(cur.lastrowid)


def set_for_note(db: Database, note_id: str, names: list[str]) -> list[str]:
    ids: list[int] = []
    for name in names:
        tag_id = _ensure(db, name)
        if tag_id is not None and tag_id not in ids:
            ids.append(tag_id)

    db.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
    if ids:
        db.executemany(
            "INSERT OR IGNORE INTO note_tags(note_id, tag_id) VALUES(?,?)",
            [(note_id, tag_id) for tag_id in ids],
        )
    db.execute(
        "DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM note_tags)"
    )
    return for_note(db, note_id)


def for_note(db: Database, note_id: str) -> list[str]:
    rows = db.query(
        "SELECT t.name FROM tags t JOIN note_tags nt ON nt.tag_id = t.id "
        "WHERE nt.note_id = ? ORDER BY t.name COLLATE NOCASE",
        (note_id,),
    )
    return [row["name"] for row in rows]


def all_tags(db: Database) -> list[dict[str, object]]:
    rows = db.query(
        "SELECT t.name, COUNT(nt.note_id) AS n FROM tags t "
        "LEFT JOIN note_tags nt ON nt.tag_id = t.id GROUP BY t.id "
        "ORDER BY n DESC, t.name COLLATE NOCASE"
    )
    return [dict(row) for row in rows]
