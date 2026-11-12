"""Export the whole knowledge base into a portable archive.

The archive is deliberately dumb: Markdown files plus JSON tables.  Anything
that can read a zip can read this, which is the point — the user's knowledge
must outlive Alan and any particular model provider.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

from ..config import Config
from ..db import Database
from ..textutil import now_iso

TABLES = (
    "raw_captures",
    "projects",
    "notes",
    "note_versions",
    "note_sources",
    "tags",
    "note_tags",
    "links",
    "conflicts",
    "lessons",
    "preferences",
    "reminders",
    "audit_log",
)


def dump_json(db: Database, path: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "exported_at": now_iso(),
        "tables": {},
    }
    for table in TABLES:
        rows = db.query(f"SELECT * FROM {table}")
        payload["tables"][table] = [dict(row) for row in rows]
    # Embeddings are derivable and huge; export their metadata only.
    payload["tables"]["embeddings_index"] = [
        dict(row) for row in db.query("SELECT note_id, model, dim, updated_at FROM embeddings")
    ]
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def export_all(db: Database, cfg: Config, *, include_audit: bool = True) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    cfg.export_dir.mkdir(parents=True, exist_ok=True)
    archive = cfg.export_dir / f"alan-export-{stamp}.zip"

    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for folder, prefix in ((cfg.raw_dir, "raw"), (cfg.vault_dir, "vault")):
            if not folder.exists():
                continue
            for path in folder.rglob("*"):
                if path.is_file():
                    zf.write(path, f"{prefix}/{path.relative_to(folder).as_posix()}")

        tables = [t for t in TABLES if include_audit or t != "audit_log"]
        payload = {"exported_at": now_iso(), "tables": {}}
        for table in tables:
            payload["tables"][table] = [dict(row) for row in db.query(f"SELECT * FROM {table}")]
        zf.writestr("index.json", json.dumps(payload, ensure_ascii=False, indent=2))

        manifest = {
            "format": "alan-knowledge-export",
            "version": 1,
            "exported_at": now_iso(),
            "counts": {table: len(rows) for table, rows in payload["tables"].items()},
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    return archive
