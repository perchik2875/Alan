"""Background loop for proactive messages.

The loop is deliberately quiet: it wakes on a fixed interval, asks the review
module for candidates, and sends at most one message per configured window.
Explicit user reminders bypass the model entirely.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Sequence

from ..config import Config
from ..core.service import AlanService
from ..store import reminders
from ..textutil import now_iso

log = logging.getLogger("alan.proactive")

SendFn = Callable[[str, str], Awaitable[None]]


class ProactiveWorker:
    def __init__(
        self,
        *,
        service: AlanService,
        cfg: Config,
        chat_ids: Sequence[str],
        send: SendFn,
    ) -> None:
        self.service = service
        self.cfg = cfg
        self.chat_ids = [str(c) for c in chat_ids]
        self.send = send
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="alan-proactive")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        interval = max(60, self.cfg.proactive.poll_minutes * 60)
        # A short initial delay keeps startup clean and lets the bot connect.
        await asyncio.sleep(20)
        while not self._stopping.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                log.exception("proactive tick failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def tick(self) -> None:
        # 1. Explicit user reminders always fire.
        for reminder in reminders.due(self.db_guard(), now=now_iso()):
            if reminder.chat_id not in self.chat_ids:
                continue
            if reminder.origin != "user":
                continue
            await self.send(reminder.chat_id, reminder.message or "Reminder, sir.")
            reminders.mark_sent(self.service.db, reminder.id)

        # 2. Considered proactive messages.
        for chat_id in self.chat_ids:
            result = await self.service.proactive_check(chat_id)
            if result and result.text.strip():
                await self.send(chat_id, result.text)

    def db_guard(self):
        return self.service.db
