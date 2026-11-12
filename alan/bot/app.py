"""Application wiring: bot construction, dependency injection, lifecycle."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from aiogram import BaseMiddleware
from typing import Any, Awaitable, Callable

from ..config import Config, require_telegram_token
from ..core.service import AlanService
from ..db import Database
from ..llm import build_embedder, build_llm
from ..proactive.worker import ProactiveWorker
from .handlers import router

log = logging.getLogger("alan.bot")

COMMANDS = [
    ("ask", "Answer from the knowledge base"),
    ("find", "Search notes"),
    ("notes", "Recent notes"),
    ("note", "Open a note"),
    ("projects", "Projects and activity"),
    ("conflicts", "Unresolved contradictions"),
    ("questions", "Open questions"),
    ("status", "Knowledge base statistics"),
    ("review", "Run a proactive review"),
    ("import", "Import an export or notes folder"),
    ("imports", "List import jobs and progress"),
    ("pause", "Pause an import"),
    ("resume", "Resume a paused import"),
    ("export", "Download the knowledge base"),
    ("private", "Mark a note private"),
    ("lessons", "Learned lessons"),
    ("policy", "Explain my operating rules"),
    ("preferences", "Stored preferences"),
    ("help", "Command list"),
]


class InjectServices(BaseMiddleware):
    def __init__(self, service: AlanService, cfg: Config) -> None:
        self.service = service
        self.cfg = cfg

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        data["service"] = self.service
        data["cfg"] = self.cfg
        return await handler(event, data)


def build_bot(cfg: Config) -> Bot:
    return Bot(
        token=require_telegram_token(cfg),
        default=DefaultBotProperties(parse_mode=None),
    )


def build_dispatcher(service: AlanService, cfg: Config) -> Dispatcher:
    dp = Dispatcher()
    middleware = InjectServices(service, cfg)
    dp.message.outer_middleware(middleware)
    dp.callback_query.outer_middleware(middleware)
    dp.include_router(router)
    return dp


async def run_bot(cfg: Config, *, db: Database | None = None) -> None:
    cfg.ensure_dirs()
    database = db or Database(cfg.db_path)
    llm = build_llm(cfg.llm)
    embedder = build_embedder(cfg.embeddings)
    service = AlanService(cfg=cfg, db=database, llm=llm, embedder=embedder)

    # v1.1: version any Markdown edits made while the bot was offline, and mark
    # orphaned import jobs as paused so /resume can pick them up safely.
    try:
        changed = service.sync_markdown()
        if changed:
            log.info("external Markdown edits detected at startup: %s", len(changed))
    except Exception:  # pragma: no cover - startup must not fail on one file
        log.exception("markdown sync at startup failed")
    from ..store import imports as imports_store

    for job in imports_store.jobs_to_resume(database):
        if job["status"] == "running":
            imports_store.set_status(database, job["id"], "paused")
            log.info("import %s marked paused after restart", job["id"])

    bot = build_bot(cfg)
    dp = build_dispatcher(service, cfg)

    async def send(chat_id: str, text: str) -> None:
        from .views import chunk

        for part in chunk(text):
            await bot.send_message(int(chat_id), part, parse_mode=None)

    chat_ids = [str(cid) for cid in cfg.owner_ids]
    worker = ProactiveWorker(service=service, cfg=cfg, chat_ids=chat_ids, send=send)
    if chat_ids and cfg.proactive.enabled:
        worker.start()
    else:
        log.info("proactive worker idle (no owner ids configured)")

    try:
        await bot.set_my_commands([BotCommand(command=name, description=desc) for name, desc in COMMANDS])
        me = await bot.get_me()
        log.info("Alan online as @%s (provider=%s model=%s embeddings=%s)",
                 me.username, cfg.llm.provider, cfg.llm.model,
                 cfg.embeddings.provider if embedder.enabled else "off")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await worker.stop()
        await llm.aclose()
        await embedder.aclose()
        await bot.session.close()
        if db is None:
            database.close()
