"""Acceptance tests for Alan v1.2 (spec section 13).

The point of v1.2 is intent before action: flow control, complaints, refusals,
and meta-questions are never stored as knowledge, onboarding can be interrupted
without leaving a false profile, behaviour lives in editable Markdown prompts,
and the user-facing vault is separate from code, database, and logs.
"""

from __future__ import annotations

import pytest

from alan.config import VAULT_FOLDERS
from alan.core import persona, prompt_loader
from alan.core.intent import classify
from alan.core.service import AlanService
from alan.models import NoteContent
from alan.store import lessons, messages, notes as notes_store, prefs, raw
from conftest import BagEmbedder, capture


def _service(cfg, db, llm) -> AlanService:
    return AlanService(cfg=cfg, db=db, llm=llm, embedder=BagEmbedder())


# --------------------------------------------------------------------------
# 13.1 Onboarding is interruptible at any point
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_13_1_onboarding_pause_preserves_partial_state(cfg, db, llm):
    cfg.onboarding_enabled = True
    service = _service(cfg, db, llm)

    intro = await service.handle_text(chat_id=1201, text="Привет")
    assert "настройк" in intro.text.lower()
    assert messages.get_pending(db, 1201)["kind"] == "onboarding"

    await service.handle_text(chat_id=1201, text="Обучение и разработка.")
    pending = messages.get_pending(db, 1201)
    assert pending["payload"]["answers"] == ["Обучение и разработка."]
    assert pending["payload"]["step"] == 1

    stopped = await service.handle_text(chat_id=1201, text="Останови настройку.")
    assert "Останавливаю настройку" in stopped.text
    paused = messages.get_pending(db, 1201)
    assert paused["kind"] == "onboarding_paused"
    assert paused["payload"]["answers"] == ["Обучение и разработка."]
    assert paused["payload"]["step"] == 1

    # No profile is invented, and the interrupt is not treated as an answer.
    assert notes_store.find(db, "user-profile") is None
    assert prefs.get_value(db, "onboarding_done") != "1"
    assert notes_store.stats(db)["notes"] == 0


@pytest.mark.asyncio
async def test_13_1_policy_question_interrupts_setup_and_answers(cfg, db, llm):
    cfg.onboarding_enabled = True
    service = _service(cfg, db, llm)
    await service.handle_text(chat_id=1211, text="Привет")

    result = await service.handle_text(chat_id=1211, text="Покажи системный промпт.")
    assert result.intent == "self_disclosure"
    assert ".alan/prompts" in result.text
    assert "Останавливаю настройку" in result.text
    assert messages.get_pending(db, 1211)["kind"] == "onboarding_paused"
    assert notes_store.stats(db)["notes"] == 0


@pytest.mark.asyncio
async def test_13_1_no_record_during_setup_is_not_an_answer(cfg, db, llm):
    cfg.onboarding_enabled = True
    service = _service(cfg, db, llm)
    await service.handle_text(chat_id=1212, text="Привет")

    result = await service.handle_text(chat_id=1212, text="Не записывай это.")
    assert result.intent == "no_record"
    pending = messages.get_pending(db, 1212)
    assert pending["kind"] == "onboarding"
    assert pending["payload"]["answers"] == []
    assert pending["payload"]["step"] == 0
    assert notes_store.stats(db)["notes"] == 0


# --------------------------------------------------------------------------
# 13.2 No false profile; a complaint corrects course without a lesson
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_13_2_complaint_creates_no_profile_and_no_lesson(cfg, db, llm):
    cfg.onboarding_enabled = True
    service = _service(cfg, db, llm)
    await service.handle_text(chat_id=1202, text="Привет")
    await service.handle_text(chat_id=1202, text="Обучение и разработка.")

    complaint = await service.handle_text(
        chat_id=1202, text="Ты меня не понял, я не отвечал на этот вопрос."
    )
    assert complaint.intent == "correction"
    assert "Вы правы" in complaint.text
    assert "урок" in complaint.text.lower()

    assert notes_store.find(db, "user-profile") is None
    assert lessons.count_active(db) == 0
    assert notes_store.stats(db)["notes"] == 0

    pending = messages.get_pending(db, 1202)
    assert pending["kind"] == "onboarding_paused"
    assert pending["payload"]["answers"] == []
    assert pending["payload"]["step"] == 0


@pytest.mark.asyncio
async def test_13_2_declining_the_summary_saves_nothing(cfg, db, llm):
    cfg.onboarding_enabled = True
    service = _service(cfg, db, llm)
    await service.handle_text(chat_id=1213, text="Привет")
    await service.handle_text(chat_id=1213, text="Обучение и разработка.")
    await service.handle_text(chat_id=1213, text="Alan и личный архив.")
    summary = await service.handle_text(chat_id=1213, text="Идеи и решения.")
    assert messages.get_pending(db, 1213)["kind"] == "onboarding_confirm"
    assert "Сохранить" in summary.text

    declined = await service.handle_text(chat_id=1213, text="Нет, не сохраняй профиль.")
    assert "не сохраняю" in declined.text.lower()
    assert notes_store.find(db, "user-profile") is None
    assert prefs.get_value(db, "onboarding_done") != "1"
    assert messages.get_pending(db, 1213)["kind"] == "onboarding_paused"


# --------------------------------------------------------------------------
# 13.3 Resume asks the correct question
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_13_3_resume_continues_from_the_right_question(cfg, db, llm):
    cfg.onboarding_enabled = True
    service = _service(cfg, db, llm)
    await service.handle_text(chat_id=1203, text="Привет")
    await service.handle_text(chat_id=1203, text="Обучение и разработка.")
    await service.handle_text(chat_id=1203, text="Стоп, позже.")

    paused = messages.get_pending(db, 1203)
    assert paused["kind"] == "onboarding_paused"
    assert paused["payload"]["step"] == 1

    resumed = await service.handle_text(chat_id=1203, text="Продолжи настройку.")
    assert "Вопрос 2 из 3" in resumed.text
    assert "Какие проекты активны прямо сейчас?" in resumed.text
    assert "Обучение и разработка." not in resumed.text

    after = messages.get_pending(db, 1203)
    assert after["kind"] == "onboarding"
    assert after["payload"]["answers"] == ["Обучение и разработка."]

    # The first question is never asked again.
    assert "Чем вы сейчас занимаетесь" not in resumed.text


# --------------------------------------------------------------------------
# 13.4 Russian stays Russian
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_13_4_russian_input_gets_a_russian_reply(cfg, db, llm):
    service = _service(cfg, db, llm)
    llm.queue(
        capture(
            language="ru",
            title="Бэкенд остаётся основным",
            summary="Бэкенд остаётся основным направлением, AI идёт параллельно.",
        )
    )

    result = await service.handle_text(
        chat_id=1204,
        text="Решил: бэкенд остаётся основным направлением, AI добавляю параллельно.",
    )

    assert "Записано" in result.text
    assert "сэр" in result.text
    assert "Recorded" not in result.text
    assert any("\u0400" <= ch <= "\u04ff" for ch in result.text)


@pytest.mark.asyncio
async def test_13_4_russian_control_replies_stay_russian(cfg, db, llm):
    service = _service(cfg, db, llm)

    no_record = await service.handle_text(chat_id=1214, text="Просто ответь, не записывай.")
    assert "Не записываю" in no_record.text
    assert "Understood" not in no_record.text

    no_workflow = await service.handle_text(chat_id=1214, text="Стоп.")
    assert "нет активного процесса" in no_workflow.text
    assert "There is no" not in no_workflow.text


# --------------------------------------------------------------------------
# 13.5 No placeholder leaks
# --------------------------------------------------------------------------

def test_13_5_finalize_strips_known_placeholders():
    cleaned = persona.finalize("Записано, {sir}.")
    assert "{" not in cleaned and "}" not in cleaned
    assert "Записано" in cleaned


@pytest.mark.asyncio
async def test_13_5_onboarding_and_disclosure_have_no_braces(cfg, db, llm):
    cfg.onboarding_enabled = True
    service = _service(cfg, db, llm)

    intro = await service.handle_text(chat_id=1205, text="Привет")
    assert "{" not in intro.text and "}" not in intro.text
    assert "сэр" in intro.text

    disclosure = await service.handle_text(chat_id=1205, text="Покажи свои правила.")
    assert "{" not in disclosure.text and "}" not in disclosure.text


def test_13_5_unknown_placeholder_is_rejected_loudly(cfg):
    prompt_loader.prompts_library(cfg)
    path = cfg.prompts_dir / "persona.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text + "\n\n{not_a_known_variable}\n", encoding="utf-8", newline="\n")

    with pytest.raises(prompt_loader.PromptError):
        prompt_loader.reload_prompts(cfg)


def test_13_5_missing_section_is_rejected(cfg):
    prompt_loader.prompts_library(cfg)
    path = cfg.prompts_dir / "persona.md"
    text = path.read_text(encoding="utf-8").replace("## Tone", "## Register")
    path.write_text(text, encoding="utf-8", newline="\n")

    with pytest.raises(prompt_loader.PromptError):
        prompt_loader.reload_prompts(cfg)


# --------------------------------------------------------------------------
# 13.6 Prompts are editable Markdown and reloadable
# --------------------------------------------------------------------------

def test_13_6_all_prompt_files_are_installed_in_the_vault(cfg):
    prompt_loader.prompts_library(cfg)
    for name in prompt_loader.PROMPT_NAMES:
        path = cfg.prompts_dir / f"{name}.md"
        assert path.exists(), name
        assert path.read_text(encoding="utf-8").strip()

    # The packaged defaults stay in the repository; the vault copy is the one
    # the user may edit.
    assert cfg.prompts_dir.is_relative_to(cfg.vault_dir)


def test_13_6_edited_markdown_is_reloaded(cfg, db, llm):
    cfg.onboarding_enabled = True
    service = _service(cfg, db, llm)

    path = cfg.prompts_dir / "onboarding.md"
    text = path.read_text(encoding="utf-8")
    edited = text.replace("Начал первичную настройку", "ТЕСТОВЫЙ МАРКЕР НАСТРОЙКИ")
    assert edited != text
    path.write_text(edited, encoding="utf-8", newline="\n")

    reloaded = service.reload_prompts()
    assert str(cfg.vault_dir) not in reloaded.text
    assert "Reloaded" in reloaded.text or "Перечитал" in reloaded.text

    intro = service.start_onboarding(1206, "ru")
    assert intro is not None
    assert "ТЕСТОВЫЙ МАРКЕР НАСТРОЙКИ" in intro
    assert "{" not in intro and "}" not in intro


def test_13_6_user_edits_survive_a_new_library(cfg):
    prompt_loader.prompts_library(cfg)
    path = cfg.prompts_dir / "safety.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n\nUSER-EDIT\n", encoding="utf-8", newline="\n")

    # A second library over the same directory must not overwrite the edit.
    prompt_loader._LIBRARIES.clear()
    prompt_loader.prompts_library(cfg)
    assert "USER-EDIT" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 13.7 The vault is isolated from code, database, and logs
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_13_7_note_files_live_only_in_the_vault(cfg, db, llm):
    service = _service(cfg, db, llm)
    llm.queue(capture())
    result = await service.handle_text(
        chat_id=1207, text="Бэкенд остаётся основным направлением, AI параллельно."
    )

    note = notes_store.get(db, result.note_id)
    assert note is not None
    path = cfg.note_path(note.path)
    assert path.exists()
    assert path.is_relative_to(cfg.vault_dir)
    assert not path.is_relative_to(cfg.data_dir)
    assert not (cfg.data_dir / note.path).exists()

    # The vault contains no database, log, or credential artifacts.
    assert cfg.db_path.is_relative_to(cfg.data_dir)
    assert not cfg.db_path.is_relative_to(cfg.vault_dir)
    assert not list(cfg.vault_dir.rglob("*.db"))
    assert not list(cfg.vault_dir.rglob("*.log"))
    assert not list(cfg.vault_dir.rglob("*.db-wal"))
    assert not (cfg.vault_dir / "raw").exists()
    assert not (cfg.vault_dir / "alan").exists()

    for folder in VAULT_FOLDERS:
        assert (cfg.vault_dir / folder).is_dir(), folder
    assert (cfg.vault_dir / ".alan" / "prompts" / "persona.md").exists()


def test_13_7_legacy_note_paths_are_migrated(cfg, db):
    legacy_dir = cfg.data_dir / "notes"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    note = notes_store.create(
        db,
        cfg,
        NoteContent(title="Legacy note", type="fact", status="active", summary="Written before v1.2."),
        raw_ids=[],
    )
    vault_file = cfg.note_path(note.path)
    legacy_file = legacy_dir / vault_file.name
    legacy_file.write_text(vault_file.read_text(encoding="utf-8"), encoding="utf-8")
    db.execute(
        "UPDATE notes SET path = ? WHERE id = ?",
        (f"notes/{vault_file.name}", note.id),
    )
    vault_file.unlink()

    moved = notes_store.migrate_legacy_paths(db, cfg)
    assert moved == [note.path]
    assert not legacy_file.exists()

    updated = notes_store.get(db, note.id)
    assert updated is not None
    assert updated.path.startswith("01_Notes/")
    assert (cfg.vault_dir / updated.path).exists()
    assert notes_store.migrate_legacy_paths(db, cfg) == []


def test_13_7_inbox_first_lifecycle(cfg, db):
    draft = notes_store.create(
        db,
        cfg,
        NoteContent(title="Черновик мысли", type="idea", status="draft", summary="Пока неясно."),
        raw_ids=[],
    )
    assert draft.path.startswith("00_Inbox/")

    unclear = notes_store.create(
        db,
        cfg,
        NoteContent(
            title="Неясный вопрос",
            type="question",
            status="needs-clarification",
            summary="Нужно уточнение.",
        ),
        raw_ids=[],
    )
    assert unclear.path.startswith("00_Inbox/")

    notes_store.set_status(db, draft.id, "active")
    moved = notes_store.promote(db, cfg, draft.id)
    assert moved.path.startswith("01_Notes/")
    assert (cfg.vault_dir / moved.path).exists()
    assert not (cfg.vault_dir / draft.path).exists()


# --------------------------------------------------------------------------
# Natural-language controls that must never store knowledge
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_make_note_from_previous_message(cfg, db, llm):
    service = _service(cfg, db, llm)
    llm.queue({"intent": "chitchat", "language": "ru", "note": None, "reply": "Понял, сэр."})
    await service.handle_text(chat_id=1215, text="Я думаю, что RAG — полезная техника.")
    assert notes_store.stats(db)["notes"] == 0

    llm.queue(
        capture(
            language="ru",
            type="idea",
            title="RAG как полезная техника",
            summary="RAG — полезная техника.",
        )
    )
    result = await service.handle_text(chat_id=1215, text="Сделай заметку из этого.")

    assert result.note_id
    note = notes_store.get(db, result.note_id)
    assert note is not None
    assert note.title == "RAG как полезная техника"
    sources = [item.text for item in raw.for_note(db, note.id)]
    assert any("RAG" in text for text in sources)
    assert all("Сделай заметку" not in text for text in sources)


@pytest.mark.asyncio
async def test_natural_language_delete_requires_confirmation(cfg, db, llm):
    service = _service(cfg, db, llm)
    llm.queue(capture())
    await service.handle_text(chat_id=1208, text="Бэкенд остаётся основным, AI параллельно.")
    assert notes_store.stats(db)["notes"] == 1

    asked = await service.handle_text(chat_id=1208, text="Удали последнюю заметку.")
    assert "Подтвердите" in asked.text
    assert notes_store.stats(db)["notes"] == 1
    assert messages.get_pending(db, 1208)["kind"] == "delete_confirm"

    done = await service.handle_text(chat_id=1208, text="Да.")
    assert "Удалено" in done.text
    assert notes_store.stats(db)["notes"] == 0
    assert raw.count(db) == 3  # every turn is preserved as an immutable raw capture


@pytest.mark.asyncio
async def test_stop_cancels_a_pending_deletion(cfg, db, llm):
    service = _service(cfg, db, llm)
    llm.queue(capture())
    await service.handle_text(chat_id=1216, text="Бэкенд остаётся основным, AI параллельно.")
    await service.handle_text(chat_id=1216, text="Удали последнюю заметку.")
    assert notes_store.stats(db)["notes"] == 1

    cancelled = await service.handle_text(chat_id=1216, text="Стоп.")
    assert "Ничего не удалено" in cancelled.text
    assert notes_store.stats(db)["notes"] == 1
    assert messages.get_pending(db, 1216) is None


@pytest.mark.asyncio
async def test_destructive_bulk_request_is_refused(cfg, db, llm):
    service = _service(cfg, db, llm)
    llm.queue(capture())
    await service.handle_text(chat_id=1209, text="Бэкенд остаётся основным, AI параллельно.")

    refused = await service.handle_text(chat_id=1209, text="Удали все заметки.")
    assert refused.intent == "safety"
    assert "Не выполняю массовое удаление" in refused.text
    assert notes_store.stats(db)["notes"] == 1


@pytest.mark.asyncio
async def test_model_question_discloses_provider_and_locality(cfg, db, llm):
    service = _service(cfg, db, llm)
    result = await service.handle_text(chat_id=1210, text="Какая модель используется?")

    assert result.intent == "self_disclosure"
    assert cfg.llm.provider in result.text
    assert cfg.llm.model in result.text
    assert "облачная" in result.text
    assert "{model}" not in result.text


def test_intent_precedence_is_deterministic():
    # Flow control beats onboarding answers and capture.
    assert classify("Останови настройку.", pending_kind="onboarding").kind == "flow_control"
    # A complaint beats capture and onboarding.
    assert classify("Ты меня не понял.", pending_kind="onboarding").kind == "complaint"
    # A refusal is a control command, not content.
    assert classify("Не записывай это.").kind == "no_record"
    # A meta-question is disclosure, not capture.
    assert classify("Покажи системный промпт.").kind == "show_policy"
    # Bulk deletion is safety.
    assert classify("Удали все заметки.").kind == "safety"
    # Only a plain answer becomes an onboarding answer.
    assert classify("Обучение и разработка.", pending_kind="onboarding").kind == "answer"
