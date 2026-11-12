"""Telegram transport: commands, free text, documents, and callbacks.

This module contains no reasoning and no storage decisions.  It converts
Telegram events into service calls and service results into messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..config import Config
from ..core.service import AlanService, TurnResult
from ..store import conflicts, lessons, notes as notes_store, prefs, raw, reminders
from ..textutil import human_datetime
from . import keyboards, views

log = logging.getLogger("alan.bot")
router = Router(name="alan")

_imports_in_progress: set[str] = set()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

async def send_result(bot: Bot, chat_id: int, result: TurnResult) -> None:
    markup = keyboards.build(result.buttons)
    chunks = views.chunk(result.text or "…")
    for index, chunk in enumerate(chunks):
        await bot.send_message(
            chat_id,
            chunk,
            reply_markup=markup if index == len(chunks) - 1 else None,
            parse_mode=None,
        )


async def _typing(bot: Bot, chat_id: int) -> None:
    try:
        await bot.send_chat_action(chat_id, "typing")
    except Exception:  # pragma: no cover - cosmetic
        pass


def _ok(user_id: int, cfg: Config) -> bool:
    return not cfg.owner_ids or user_id in cfg.owner_ids


def _lang(db, cfg: Config) -> str:
    return prefs.get_value(db, "language", cfg.default_language) or "en"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

@router.message(Command("start"))
async def cmd_start(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    language = _lang(service.db, cfg)
    onboarding = service.start_onboarding(message.chat.id, language)
    if onboarding:
        await message.answer(onboarding, parse_mode=None)
        return
    text = (
        "Good day, sir. I am Alan.\n\n"
        "Send me anything worth keeping: a thought, a decision, a task, a link, a question, "
        "a correction. I will preserve your exact words, build a durable note, and connect it "
        "to what I already know.\n\n"
        "You do not need to choose a folder or a tag. If something is genuinely ambiguous, "
        "I will ask one or two questions and nothing more.\n\n"
        "/help for the full command list."
    )
    await message.answer(text, parse_mode=None)


@router.message(Command("help"))
async def cmd_help(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    text = (
        "Commands, sir:\n\n"
        "/ask <question> — answer from the knowledge base\n"
        "/find <terms> — search notes\n"
        "/notes [type] — recent notes\n"
        "/note <id|slug|title> — open a note\n"
        "/raw <note> — original captures behind a note\n"
        "/versions <note> — version history\n"
        "/projects — projects and activity\n"
        "/project <name> — notes in a project\n"
        "/conflicts — unresolved contradictions\n"
        "/questions — open questions\n"
        "/status — knowledge base statistics\n"
        "/review — run a proactive review now\n"
        "/reflect — extract lessons from recent turns\n"
        "/lessons — learned lessons\n"
        "/forget <lesson-id> — deactivate a lesson\n"
        "/policy — explain my operating rules and configuration\n"
        "/prompts — show the prompt folder and reload edited prompts\n"
        "/preferences — stored preferences\n"
        "/pref <key> <value> — set a preference\n"
        "/unpref <key> — remove a preference\n"
        "/private <note> — mark private and exclude from retrieval\n"
        "/unprivate <note> — remove that restriction\n"
        "/delete <note> — delete a note (confirmation required)\n"
        "/export — download the entire knowledge base\n"
        "/import — import a Telegram export or a notes folder\n"
        "/imports — list import jobs and progress\n"
        "/pause <job-id> — pause an import\n"
        "/resume <job-id> — resume an import\n"
        "/cancel — cancel a pending clarification or import\n\n"
        "You do not need commands: “покажи системный промпт”, “останови настройку”, "
        "“продолжи настройку”, “не записывай это”, and “удали последнюю заметку” work directly. "
        "Other substantive messages are captured as-is."
    )
    await message.answer(text, parse_mode=None)


@router.message(Command("status"))
async def cmd_status(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    stats = notes_store.stats(service.db)
    pending = reminders.for_chat(service.db, message.chat.id)
    extra = f"\n- pending reminders: {len(pending)}" if pending else ""
    await message.answer(views.stats_block(stats) + extra, parse_mode=None)


@router.message(Command("notes"))
async def cmd_notes(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    note_type = (command.args or "").strip() or None
    found = notes_store.list_notes(service.db, limit=15, type=note_type, include_sensitive=False)
    await message.answer(views.note_list(found, header="Recent notes, sir:"), parse_mode=None)


@router.message(Command("note"))
async def cmd_note(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    ref = (command.args or "").strip()
    if not ref:
        await message.answer("Which note, sir? /note <id|slug|title>", parse_mode=None)
        return
    note = notes_store.find(service.db, ref)
    if note is None:
        await message.answer("No such note, sir.", parse_mode=None)
        return
    await _send_note(message.bot, message.chat.id, service, note, cfg)


async def _send_note(bot: Bot, chat_id: int, service: AlanService, note, cfg: Config) -> None:
    from ..store import links as links_store

    body = views.note_card(note)
    related = []
    for link in links_store.outgoing(service.db, note.id):
        target = notes_store.get(service.db, link.to_note_id)
        if target:
            related.append({"slug": target.slug, "title": target.title})
    for link in links_store.incoming(service.db, note.id):
        source = notes_store.get(service.db, link.from_note_id)
        if source:
            related.append({"slug": source.slug, "title": source.title})
    if related:
        body += "\n\nLinks:\n" + views.note_links(related)
    sources = raw.for_note(service.db, note.id)
    if sources:
        body += "\n\nSource:\n" + views.note_sources(sources)

    buttons = [
        [InlineKeyboardButton(text="Raw capture", callback_data=f"note:raw:{note.id}")],
        [
            InlineKeyboardButton(
                text="Unmark private" if note.sensitive else "Mark private",
                callback_data=f"note:privacy:{note.id}",
            ),
            InlineKeyboardButton(text="Delete", callback_data=f"note:delete:{note.id}"),
        ],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    for index, chunk in enumerate(views.chunk(body)):
        await bot.send_message(
            chat_id, chunk, parse_mode=None,
            reply_markup=markup if index == 0 else None,
        )


@router.message(Command("raw"))
async def cmd_raw(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    ref = (command.args or "").strip()
    if not ref:
        await message.answer("Which record, sir? /raw <note>", parse_mode=None)
        return
    captures = []
    if ref.startswith("r_"):
        capture = raw.get(service.db, ref)
        captures = [capture] if capture else []
    else:
        note = notes_store.find(service.db, ref)
        if note:
            captures = raw.for_note(service.db, note.id)
    if not captures:
        await message.answer("No raw capture found, sir.", parse_mode=None)
        return
    for capture in captures:
        header = (
            f"Raw capture {capture.id}\n"
            f"{human_datetime(capture.received_at)} · {capture.source} · {capture.language}\n"
            f"{capture.path}\n\n"
        )
        for chunk in views.chunk(header + capture.text):
            await message.answer(chunk, parse_mode=None)


@router.message(Command("versions"))
async def cmd_versions(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    note = notes_store.find(service.db, (command.args or "").strip())
    if note is None:
        await message.answer("No such note, sir.", parse_mode=None)
        return
    rows = notes_store.versions(service.db, note.id)
    lines = [f"{note.title} — versions, sir:"]
    for row in rows:
        lines.append(f"- v{row['version']} · {row['created_at'][:16]} · {row['reason']}")
    await message.answer("\n".join(lines), parse_mode=None)


@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer("What should I look for, sir?", parse_mode=None)
        return
    await _typing(message.bot, message.chat.id)
    hits = await service.search_notes(query)
    if not hits:
        from ..core import persona

        await message.answer(persona.nothing_found("ru" if _lang(service.db, cfg).startswith("ru") else "en"), parse_mode=None)
        return
    lines = ["Best matches, sir:"]
    for hit in hits:
        methods = "/".join(hit.methods)
        lines.append(f"- {hit.note.title} · {hit.note.type}/{hit.note.status} · {hit.note.slug} ({methods})")
    await message.answer("\n".join(lines), parse_mode=None)


@router.message(Command("projects"))
async def cmd_projects(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    from ..store import projects as projects_store

    rows = projects_store.counts(service.db)
    if not rows:
        await message.answer("No projects yet, sir.", parse_mode=None)
        return
    lines = ["Projects, sir:"]
    for row in rows:
        if not row["confirmed"]:
            continue
        last = (row["last_updated"] or "never")[:10]
        lines.append(f"- {row['name']} · {row['note_count']} notes · last {last} · {row['status']}")
    await message.answer("\n".join(lines), parse_mode=None)


@router.message(Command("project"))
async def cmd_project(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    from ..store import projects as projects_store

    name = (command.args or "").strip()
    project = projects_store.find_by_name(service.db, name) if name else None
    if project is None:
        await message.answer("No such project, sir. /projects", parse_mode=None)
        return
    found = notes_store.list_notes(service.db, limit=30, project_id=project.id, include_sensitive=False)
    await message.answer(
        views.note_list(found, header=f"{project.name}:"), parse_mode=None
    )


@router.message(Command("conflicts"))
async def cmd_conflicts(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    rows = conflicts.unresolved(service.db)
    if not rows:
        await message.answer("No unresolved conflicts, sir.", parse_mode=None)
        return
    lines = ["Unresolved conflicts, sir:"]
    for row in rows[:10]:
        lines.append(f"- {row['note_title']} ⇄ {row['other_title']} · {row['id']}")
    await message.answer("\n".join(lines), parse_mode=None)


@router.message(Command("questions"))
async def cmd_questions(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    found = notes_store.list_notes(service.db, limit=30, type="question", status="unresolved")
    await message.answer(
        views.note_list(found, header="Open questions, sir:"), parse_mode=None
    )


@router.message(Command("lessons"))
async def cmd_lessons(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    rows = lessons.list_active(service.db)
    if not rows:
        await message.answer("No lessons recorded, sir.", parse_mode=None)
        return
    lines = ["Learned lessons, sir:"]
    for row in rows:
        lines.append(
            f"- {row.id} · [{row.kind}] {row.text} "
            f"(scope {row.scope}, confidence {row.confidence:.2f}, source {row.evidence_raw_id or 'none'})"
        )
        if row.explanation:
            lines.append(f"  why: {row.explanation}")
    await message.answer("\n".join(lines), parse_mode=None)


@router.message(Command("forget"))
async def cmd_forget(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    lid = (command.args or "").strip()
    if not lid:
        await message.answer("Which lesson, sir? /forget <lesson-id>", parse_mode=None)
        return
    ok = lessons.deactivate(service.db, lid)
    await message.answer("Forgotten, sir." if ok else "No such lesson, sir.", parse_mode=None)


@router.message(Command("preferences"))
async def cmd_preferences(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    values = prefs.all_active(service.db)
    if not values:
        await message.answer(
            "No stored preferences, sir. Try /pref detail short, /pref language ru, "
            "/pref quiet_hours 23-8, /pref proactive off.",
            parse_mode=None,
        )
        return
    lines = ["Stored preferences, sir:"] + [f"- {k}: {v}" for k, v in values.items()]
    await message.answer("\n".join(lines), parse_mode=None)


@router.message(Command("pref"))
async def cmd_pref(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Usage: /pref <key> <value>", parse_mode=None)
        return
    prefs.set_value(service.db, parts[0], parts[1])
    await message.answer(f"Preference set, sir: {parts[0]} = {parts[1]}.", parse_mode=None)


@router.message(Command("unpref"))
async def cmd_unpref(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    key = (command.args or "").strip()
    ok = prefs.remove(service.db, key) if key else False
    await message.answer("Removed, sir." if ok else "No such preference, sir.", parse_mode=None)


@router.message(Command("private"))
async def cmd_private(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    await _privacy(message, command, service, cfg, True)


@router.message(Command("unprivate"))
async def cmd_unprivate(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    await _privacy(message, command, service, cfg, False)


async def _privacy(message: Message, command: CommandObject, service: AlanService, cfg: Config, private: bool) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    note = notes_store.find(service.db, (command.args or "").strip())
    if note is None:
        await message.answer("No such note, sir.", parse_mode=None)
        return
    result = service.set_note_private(note.id, private)
    await send_result(message.bot, message.chat.id, result)


@router.message(Command("delete"))
async def cmd_delete(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    note = notes_store.find(service.db, (command.args or "").strip())
    if note is None:
        await message.answer("No such note, sir.", parse_mode=None)
        return
    await message.answer(
        f"Delete “{note.title}” permanently, sir?\nThe raw capture will remain.",
        reply_markup=keyboards.confirm_delete("note", note.id),
        parse_mode=None,
    )


@router.message(Command("export"))
async def cmd_export(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    await _typing(message.bot, message.chat.id)
    archive = service.export_archive()
    await message.answer_document(FSInputFile(archive), caption="Full export, sir.", parse_mode=None)


@router.message(Command("review"))
async def cmd_review(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    await _typing(message.bot, message.chat.id)
    result = await service.proactive_check(message.chat.id)
    if result is None:
        await message.answer("Nothing worth interrupting you for, sir.", parse_mode=None)
        return
    await send_result(message.bot, message.chat.id, result)


@router.message(Command("reflect"))
async def cmd_reflect(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    await _typing(message.bot, message.chat.id)
    text = await service.reflect(message.chat.id)
    await message.answer(text, parse_mode=None)


@router.message(Command("policy"))
async def cmd_policy(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    language = _lang(service.db, cfg)
    await message.answer(service.policy_reply(language), parse_mode=None)


@router.message(Command("prompts"))
async def cmd_prompts(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    result = service.reload_prompts()
    from ..core import persona

    await message.answer(
        persona.finalize(
            f"{result.text}\n\n`{cfg.vault_dir.name}/.alan/prompts/` — persona.md, capture.md, "
            "clarification.md, retrieval.md, proactive.md, onboarding.md, conflict.md, "
            "learning.md, safety.md.",
            data_dir=cfg.data_dir,
        ),
        parse_mode=None,
    )


def messages_count(db) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM messages") or 0)


@router.message(Command("imports"))
async def cmd_imports(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    jobs = service.import_jobs(message.chat.id)
    if not jobs:
        await message.answer("No import jobs, sir.", parse_mode=None)
        return
    lines = ["Import jobs, sir:"]
    for job in jobs:
        lines.append(
            f"- {job['id']} · {job['status']} · {job['captured']}/{job['total']} items · "
            f"notes {job['notes_made']} · duplicates {job['duplicates']} · conflicts {job['conflicts']}"
        )
    await message.answer("\n".join(lines), parse_mode=None)


@router.message(Command("pause"))
async def cmd_pause(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    job_id = (command.args or "").strip()
    if not job_id:
        jobs = [j for j in service.import_jobs(message.chat.id) if j["status"] in {"running", "pending"}]
        job_id = jobs[0]["id"] if jobs else ""
    if job_id and service.pause_import(job_id):
        _imports_in_progress.discard(str(message.chat.id))
        await message.answer(f"Import paused, sir: {job_id}. Raw captures already stored are safe.", parse_mode=None)
        return
    await message.answer("No running import to pause, sir.", parse_mode=None)


@router.message(Command("resume"))
async def cmd_resume(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    job_id = (command.args or "").strip()
    if not job_id:
        jobs = [j for j in service.import_jobs(message.chat.id) if j["status"] == "paused"]
        job_id = jobs[0]["id"] if jobs else ""
    if not job_id:
        await message.answer("No paused import to resume, sir.", parse_mode=None)
        return
    await message.answer(f"Resuming import, sir: {job_id}.", parse_mode=None)
    asyncio.create_task(_run_import_job(message.bot, service, message.chat.id, job_id, resume=True))


@router.message(Command("cancel"))
async def cmd_cancel(
    message: Message, command: CommandObject, service: AlanService, cfg: Config
) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    from ..store import messages as messages_store

    job_id = (command.args or "").strip()
    if job_id:
        if service.cancel_import(job_id):
            _imports_in_progress.discard(str(message.chat.id))
            await message.answer(
                f"Import cancelled, sir: {job_id}. Raw captures already stored are kept.",
                parse_mode=None,
            )
        else:
            await message.answer(f"No active import with that id, sir: {job_id}.", parse_mode=None)
        return

    pending = messages_store.get_pending(service.db, message.chat.id)
    if pending is not None:
        messages_store.clear_pending(service.db, message.chat.id)
        await message.answer("Cancelled, sir. The original message is preserved.", parse_mode=None)
        return

    active = [
        job
        for job in service.import_jobs(message.chat.id)
        if job["status"] in {"pending", "running", "paused"}
    ]
    if active:
        service.cancel_import(active[0]["id"])
        _imports_in_progress.discard(str(message.chat.id))
        await message.answer(
            f"Import cancelled, sir: {active[0]['id']}. Raw captures already stored are kept.",
            parse_mode=None,
        )
        return

    await message.answer("Nothing to cancel, sir.", parse_mode=None)


@router.message(Command("ask"))
async def cmd_ask(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    question = (command.args or "").strip()
    if not question:
        await message.answer("Ask what, sir?", parse_mode=None)
        return
    await _typing(message.bot, message.chat.id)
    result = await service.handle_text(chat_id=message.chat.id, text=question, message_id=message.message_id)
    await send_result(message.bot, message.chat.id, result)


@router.message(Command("import"))
async def cmd_import(message: Message, command: CommandObject, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return

    target: Path | None = None
    arg = (command.args or "").strip()
    if arg:
        target = Path(arg).expanduser()
        if not target.exists():
            await message.answer(f"No such path, sir: {arg}", parse_mode=None)
            return
    elif message.reply_to_message and message.reply_to_message.document:
        target = await _download(message, message.reply_to_message.document, cfg)

    if target is None:
        await message.answer(
            "Send me a Telegram export (result.json) or an .md/.txt file and reply to it with /import, "
            "or use /import <local path>.\n\n"
            "The originals are stored first; nothing is summarised away.",
            parse_mode=None,
        )
        return

    key = str(message.chat.id)
    if key in _imports_in_progress:
        await message.answer("An import is already running, sir.", parse_mode=None)
        return

    await message.answer(
        "Import started, sir. Original text is stored before anything is indexed. "
        "I will report back with the inventory.",
        parse_mode=None,
    )
    asyncio.create_task(_run_import(message.bot, service, message.chat.id, target))


async def _run_import(bot: Bot, service: AlanService, chat_id: int, path: Path) -> None:
    key = str(chat_id)
    _imports_in_progress.add(key)

    async def progress(text: str) -> None:
        try:
            await bot.send_message(chat_id, text, parse_mode=None)
        except Exception:  # pragma: no cover
            pass

    try:
        job = service.start_import(path, chat_id=chat_id, progress=progress)
        await bot.send_message(
            chat_id,
            f"Original text stored, sir. Import job {job['id']} is ready.\n"
            f"Use /imports to inspect it, /pause to stop, /resume to continue.",
            parse_mode=None,
        )
        await _run_import_job(bot, service, chat_id, job["id"], progress=progress)
    except Exception as exc:  # pragma: no cover - background safety
        log.exception("import failed")
        await bot.send_message(chat_id, f"Import failed, sir: {exc}", parse_mode=None)
    finally:
        _imports_in_progress.discard(key)


async def _run_import_job(
    bot: Bot,
    service: AlanService,
    chat_id: int,
    job_id: str,
    *,
    resume: bool = False,
    progress=None,
) -> None:
    key = str(chat_id)
    _imports_in_progress.add(key)
    if progress is None:
        async def progress(text: str) -> None:
            try:
                await bot.send_message(chat_id, text, parse_mode=None)
            except Exception:  # pragma: no cover
                pass

    try:
        if resume:
            result = await service.resume_import(job_id, progress=progress, chat_id=chat_id)
        else:
            result = await service.process_import(job_id, progress=progress, chat_id=chat_id)
        summary = (
            f"Import {result.job_id} complete, sir.\n"
            f"- items read: {result.items}\n"
            f"- original captures stored: {result.captures}\n"
            f"- notes created: {result.notes}\n"
            f"- possible duplicates: {result.duplicates}\n"
            f"- conflicts: {result.conflicts}"
        )
        for chunk in views.chunk(summary):
            await bot.send_message(chat_id, chunk, parse_mode=None)
    except Exception as exc:  # pragma: no cover - background safety
        log.exception("import failed")
        await bot.send_message(chat_id, f"Import failed, sir: {exc}", parse_mode=None)
    finally:
        _imports_in_progress.discard(key)


async def _download(message: Message, document, cfg: Config) -> Path | None:
    if document.file_size and document.file_size > 20 * 1024 * 1024:
        await message.answer("That file is over 20 MB, sir. Telegram will not let me fetch it.", parse_mode=None)
        return None
    cfg.incoming_dir.mkdir(parents=True, exist_ok=True)
    target = cfg.incoming_dir / f"{message.message_id}_{document.file_name or 'upload'}"
    try:
        await message.bot.download(document, destination=target)
    except Exception as exc:  # pragma: no cover
        log.warning("download failed: %s", exc)
        await message.answer("I could not download that file, sir.", parse_mode=None)
        return None
    return target


# --------------------------------------------------------------------------
# Free text
# --------------------------------------------------------------------------

@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    from ..store import messages as messages_store

    pending = messages_store.get_pending(service.db, message.chat.id)
    if pending and pending["kind"] == "project_rename":
        payload = pending["payload"] or {}
        result = service.confirm_project(payload.get("project_id", ""), name=message.text.strip())
        messages_store.clear_pending(service.db, message.chat.id)
        await send_result(message.bot, message.chat.id, result)
        return

    await _typing(message.bot, message.chat.id)
    result = await service.handle_text(
        chat_id=message.chat.id,
        text=message.text,
        message_id=message.message_id,
    )
    await send_result(message.bot, message.chat.id, result)


@router.message(F.photo | F.document | F.voice | F.audio | F.video)
async def on_media(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return

    document = message.document
    if document and (document.file_name or "").lower().endswith((".json", ".md", ".markdown", ".txt")):
        target = await _download(message, document, cfg)
        if target is None:
            return
        key = str(message.chat.id)
        if key in _imports_in_progress:
            await message.answer("An import is already running, sir.", parse_mode=None)
            return
        await message.answer("Import started, sir.", parse_mode=None)
        asyncio.create_task(_run_import(message.bot, service, message.chat.id, target))
        return

    media = []
    if message.photo:
        media.append({"type": "photo", "file_id": message.photo[-1].file_id})
    if document:
        media.append({"type": "document", "file_name": document.file_name, "file_id": document.file_id})
    if message.voice:
        media.append({"type": "voice", "file_id": message.voice.file_id})
    if message.audio:
        media.append({"type": "audio", "file_id": message.audio.file_id})
    if message.video:
        media.append({"type": "video", "file_id": message.video.file_id})

    caption = message.caption or ""
    text = caption or f"[{', '.join(m['type'] for m in media)}]"
    result = await service.handle_text(
        chat_id=message.chat.id,
        text=text,
        message_id=message.message_id,
        media=media,
    )
    await send_result(message.bot, message.chat.id, result)
    if message.voice:
        await message.answer(
            "Stored, sir, but voice transcription is outside the current scope. Send the text and I will build a note.",
            parse_mode=None,
        )
    elif message.photo or message.video:
        await message.answer(
            "Stored with its reference, sir. I do not read image or video content yet.",
            parse_mode=None,
        )


# --------------------------------------------------------------------------
# Callbacks
# --------------------------------------------------------------------------

@router.callback_query()
async def on_callback(query: CallbackQuery, service: AlanService, cfg: Config) -> None:
    if not _ok(query.from_user.id, cfg):
        await query.answer()
        return

    data = query.data or ""
    parts = data.split(":")
    action = parts[0] if parts else ""

    try:
        if action == "link" and len(parts) >= 3:
            result = service.confirm_link(parts[2]) if parts[1] == "confirm" else service.reject_link(parts[2])
        elif action == "conflict" and len(parts) >= 3:
            if parts[1] == "show":
                result = service.show_conflict(parts[2])
            else:
                result = service.resolve_conflict(parts[2], parts[1])
        elif action == "project" and len(parts) >= 3:
            if parts[1] == "confirm":
                result = service.confirm_project(parts[2])
            elif parts[1] == "rename":
                from ..store import messages as messages_store

                messages_store.set_pending(
                    service.db,
                    chat_id=query.message.chat.id,
                    kind="project_rename",
                    payload={"project_id": parts[2]},
                    expires_at=(
                        datetime.now(timezone.utc) + timedelta(minutes=30)
                    ).isoformat(timespec="seconds"),
                )
                await query.answer()
                await query.message.answer("What should the project be called, sir?", parse_mode=None)
                return
            else:
                result = TurnResult(text="Unknown action, sir.", error=True)
        elif action == "note" and len(parts) >= 3:
            result = await _note_callback(query, service, cfg, parts[1], parts[2])
            if result is None:
                await query.answer()
                return
        elif action == "metadata" and len(parts) >= 3:
            if parts[1] == "refresh":
                result = service.refresh_metadata(parts[2])
            else:
                result = service.keep_metadata(parts[2])
        elif action == "delete":
            if parts[1] == "cancel":
                result = TurnResult(text="Cancelled, sir.")
            elif parts[1] == "note":
                result = service.delete_note(parts[2])
            else:
                result = TurnResult(text="Unknown action, sir.", error=True)
        else:
            result = TurnResult(text="Unknown action, sir.", error=True)
    except Exception:
        log.exception("callback failed: %s", data)
        result = TurnResult(text="That action failed, sir.", error=True)

    await query.answer()
    await send_result(query.bot, query.message.chat.id, result)


async def _note_callback(query: CallbackQuery, service: AlanService, cfg: Config, verb: str, note_id: str):
    note = notes_store.get(service.db, note_id)
    if note is None:
        await query.answer("No such note, sir.", show_alert=True)
        return None

    if verb == "raw":
        captures = raw.for_note(service.db, note.id)
        if not captures:
            await query.message.answer("No raw capture attached, sir.", parse_mode=None)
            return None
        capture = captures[-1]
        await query.message.answer(
            f"Raw capture {capture.id} · {human_datetime(capture.received_at)}\n\n{capture.text}",
            parse_mode=None,
        )
        return None

    if verb == "privacy":
        return service.set_note_private(note.id, not note.sensitive)

    if verb == "delete":
        await query.message.answer(
            f"Delete “{note.title}” permanently, sir?\nThe raw capture will remain.",
            reply_markup=keyboards.confirm_delete("note", note.id),
            parse_mode=None,
        )
        return None

    if verb == "show":
        await _send_note(query.bot, query.message.chat.id, service, note, cfg)
        return None

    return TurnResult(text="Unknown action, sir.", error=True)


# --------------------------------------------------------------------------
# Unknown commands
# --------------------------------------------------------------------------

@router.message(F.text.startswith("/"))
async def on_unknown_command(message: Message, service: AlanService, cfg: Config) -> None:
    if not _ok(message.from_user.id, cfg):
        return
    await message.answer(
        "Unknown command, sir. /help lists what I can do.",
        parse_mode=None,
    )
