"""Importing existing material and exporting it back out."""

from __future__ import annotations

import json
import zipfile

import pytest

from alan.ingest import markdown, telegram
from alan.models import NoteContent
from alan.store import export as export_store
from alan.store import notes as notes_store
from alan.store import raw

from conftest import capture


def _export(tmp_path):
    payload = {
        "name": "Saved Messages",
        "type": "saved_messages",
        "messages": [
            {"id": 1, "type": "message", "date": "2024-03-01T10:00:00", "from": "Me", "text": "Привет"},
            {"id": 2, "type": "service", "date": "2024-03-01T10:01:00", "text": "joined"},
            {
                "id": 3,
                "type": "message",
                "date": "2024-03-01T10:05:00",
                "from": "Me",
                "text": [{"type": "bold", "text": "Backend"}, " stays primary"],
            },
            {"id": 4, "type": "message", "date": "2024-03-02T09:00:00", "text": "", "photo": "photo.jpg"},
        ],
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_telegram_export_is_parsed_without_loss(tmp_path):
    items = telegram.read_export(_export(tmp_path))
    texts = [item.text for item in items]
    assert "Привет" in texts
    assert "Backend stays primary" in texts
    assert any("photo" in text for text in texts)
    assert all(item.received_at.endswith("+00:00") for item in items)
    assert not any("joined" in text for text in texts)


def test_markdown_folder_import(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".obsidian" / "workspace.md").write_text("junk", encoding="utf-8")
    (vault / "Decision.md").write_text(
        "---\ntitle: Learning plan decision\ncreated: 2025-01-02\ntags: [learning]\n---\n\n"
        "Backend stays primary.\n",
        encoding="utf-8",
    )
    (vault / "note.txt").write_text("Plain thought.", encoding="utf-8")

    items = markdown.read_folder(vault)
    assert len(items) == 2
    titles = {item.text.splitlines()[0] for item in items}
    assert any("Learning plan decision" in title for title in titles)


def test_export_archive_contains_files_and_manifest(cfg, db):
    capture = raw.create(db, cfg, chat_id=1, text="Original words.")
    notes_store.create(
        db, cfg,
        NoteContent(title="Exported note", type="fact", status="active", summary="Something durable."),
        raw_ids=[capture.id],
    )

    archive = export_store.export_all(db, cfg)
    assert archive.exists()

    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        assert "index.json" in names
        assert any(name.startswith("vault/") for name in names)
        assert any(name.startswith("raw/") for name in names)

        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["format"] == "alan-knowledge-export"
        index = json.loads(zf.read("index.json"))
        assert len(index["tables"]["notes"]) == 1
        assert index["tables"]["raw_captures"][0]["text"] == "Original words."


@pytest.mark.asyncio
async def test_import_pipeline_stores_captures_and_reports_inventory(service, db, llm, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# Backend priority\n\n" + "Backend stays primary. " * 6, encoding="utf-8")
    (vault / "b.md").write_text("# AI track\n\n" + "AI becomes a parallel track. " * 6, encoding="utf-8")

    llm.queue(capture(title="Backend priority", project="Learning plan"))
    llm.queue(capture(title="AI track", project="Learning plan"))
    llm.queue(
        {
            "summary": "Two related notes about the learning plan.",
            "themes": [{"name": "Learning", "description": "Priorities and capacity.", "note_count": 2}],
            "projects": ["Learning plan"],
            "suggested_projects": ["Learning plan"],
            "first_step": "Review the learning notes.",
        }
    )

    result = await service.import_path(vault, chat_id=1, limit=10)

    assert result.items == 2
    assert result.captures == 2
    assert result.notes == 2
    assert "learning plan" in result.inventory.lower()
    assert raw.count(db) == 2
    assert notes_store.stats(db)["notes"] == 2
