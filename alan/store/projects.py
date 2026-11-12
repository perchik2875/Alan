"""Projects and topics.

Alan may *propose* a project, but a new permanent taxonomy is only created
with the user's agreement.  Unconfirmed projects are therefore stored with
``confirmed = 0`` and are labelled as provisional wherever they appear.
"""

from __future__ import annotations

import re

from ..db import Database
from ..models import Project
from ..textutil import now_iso, slugify
from . import audit
from .ids import project_id as new_project_id


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


def create(
    db: Database,
    name: str,
    *,
    description: str = "",
    confirmed: bool = True,
    actor: str = "alan",
) -> Project:
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        name = "Untitled project"

    existing = find_by_name(db, name)
    if existing is not None:
        return existing

    slug = slugify(name, fallback="project")
    base = slug
    counter = 2
    while db.query_one("SELECT 1 FROM projects WHERE slug = ?", (slug,)):
        slug = f"{base}-{counter}"
        counter += 1

    pid = new_project_id()
    ts = now_iso()
    db.execute(
        "INSERT INTO projects(id, name, slug, description, status, confirmed, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (pid, name, slug, description, "active", 1 if confirmed else 0, ts, ts),
    )
    audit.record(
        db,
        "project.create",
        actor=actor,
        target=pid,
        details={"name": name, "confirmed": confirmed},
    )
    return get(db, pid)  # type: ignore[return-value]


def get(db: Database, pid: str) -> Project | None:
    row = db.query_one("SELECT * FROM projects WHERE id = ?", (pid,))
    return Project.from_row(row) if row else None


def get_by_slug(db: Database, slug: str) -> Project | None:
    row = db.query_one("SELECT * FROM projects WHERE slug = ?", (slug,))
    return Project.from_row(row) if row else None


def find_by_name(db: Database, name: str) -> Project | None:
    target = _norm(name)
    for row in db.query("SELECT * FROM projects"):
        if _norm(row["name"]) == target or row["slug"] == slugify(name):
            return Project.from_row(row)
    return None


def resolve(db: Database, name: str, *, create_if_missing: bool = True, confirmed: bool = True) -> Project | None:
    """Return the project matching ``name``, creating a provisional one if needed."""

    found = find_by_name(db, name)
    if found is not None:
        return found
    if not create_if_missing:
        return None
    return create(db, name, confirmed=confirmed)


def confirm(db: Database, pid: str, *, name: str | None = None, actor: str = "user") -> Project | None:
    project = get(db, pid)
    if project is None:
        return None
    new_name = (name or project.name).strip() or project.name
    new_slug = project.slug
    if _norm(new_name) != _norm(project.name):
        candidate = slugify(new_name, fallback="project")
        suffix = 2
        base = candidate
        while db.query_one("SELECT 1 FROM projects WHERE slug = ? AND id <> ?", (candidate, pid)):
            candidate = f"{base}-{suffix}"
            suffix += 1
        new_slug = candidate
    db.execute(
        "UPDATE projects SET name = ?, slug = ?, confirmed = 1, updated_at = ? WHERE id = ?",
        (new_name, new_slug, now_iso(), pid),
    )
    audit.record(db, "project.confirm", actor=actor, target=pid, details={"name": new_name})
    return get(db, pid)


def list_all(db: Database, *, include_provisional: bool = False, status: str | None = None) -> list[Project]:
    sql = "SELECT * FROM projects WHERE 1=1"
    params: list[object] = []
    if not include_provisional:
        sql += " AND confirmed = 1"
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY name COLLATE NOCASE"
    return [Project.from_row(row) for row in db.query(sql, params)]


def set_status(db: Database, pid: str, status: str, *, actor: str = "user") -> None:
    db.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), pid))
    audit.record(db, "project.status", actor=actor, target=pid, details={"status": status})


def counts(db: Database) -> list[dict[str, object]]:
    rows = db.query(
        """
        SELECT p.id, p.name, p.slug, p.status, p.confirmed,
               COUNT(n.id) AS note_count,
               MAX(n.updated_at) AS last_updated
        FROM projects p
        LEFT JOIN notes n ON n.project_id = p.id
        GROUP BY p.id
        ORDER BY last_updated DESC NULLS LAST, p.name COLLATE NOCASE
        """
    )
    return [dict(row) for row in rows]
