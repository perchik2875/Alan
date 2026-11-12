"""The Alan service: one facade used by the Telegram bot and the CLI.

Everything a turn needs lives here: raw capture, retrieval, assessment,
deterministic application, answer composition, and learning.  The bot layer is
only transport and rendering; the CLI is only another transport.

v1.1 adds four things in this layer:

* persisted working memory and deterministic answers about the conversation;
* honest self-disclosure of policy and configuration (never secrets);
* strict lesson discipline: questions and complaints do not become rules;
* resumable, observable imports and Markdown-external-edit handling.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Awaitable, Callable, Sequence

from ..config import Config
from ..db import Database
from ..errors import AlanError, ConfigError, EmbeddingError, LLMError
from ..llm.embeddings import Embedder
from ..models import Note, NoteContent, SearchHit
from ..store import (
    conflicts,
    export as export_store,
    imports as imports_store,
    lessons,
    links,
    messages,
    notes as notes_store,
    prefs,
    projects as projects_store,
    raw,
    reminders,
    search,
    tags,
)
from ..textutil import detect_language, one_line, now_iso, utcnow
from . import apply as apply_mod
from . import intent as intent_mod
from . import persona
from . import prompt_loader
from .analyze import Reasoner, safe_llm_error
from .prompts import PromptSet
from .schemas import Assessment

log = logging.getLogger("alan.service")

ProgressFn = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class Button:
    label: str
    data: str


@dataclass(slots=True)
class TurnResult:
    text: str = ""
    buttons: list[list[Button]] = field(default_factory=list)
    note_id: str | None = None
    intent: str = ""
    error: bool = False


@dataclass(slots=True)
class ImportResult:
    items: int
    captures: int
    notes: int
    inventory: str = ""
    duplicates: int = 0
    conflicts: int = 0
    job_id: str = ""


_YES = re.compile(r"^\s*(да|yes|конечно|верно|ага|подтверждаю|sure|ok|okay|yep|угу)\b", re.IGNORECASE)
_NO = re.compile(r"^\s*(нет|no|nope|не надо|не нужно|не стоит)\b", re.IGNORECASE)

_MEMORY_TRIGGERS = (
    "что я только что сказал", "что я только что написал", "что я сейчас сказал",
    "какое моё прошлое сообщение", "какое мое прошлое сообщение",
    "какое моё предыдущее сообщение", "какое мое предыдущее сообщение",
    "что ты помнишь", "что помнишь", "объясни свой предыдущий ответ",
    "объясни свое предыдущий ответ", "объясни предыдущий ответ",
    "я имел в виду предыдущее", "я имел в виду прошлое",
    "what did i just say", "what did i say", "what was my previous question",
    "what was my last question", "what was my previous message", "what was my last message",
    "what do you remember", "explain your previous answer", "explain your last answer",
    "i meant the previous", "i meant the last message",
)


class AlanService:
    def __init__(
        self,
        *,
        cfg: Config,
        db: Database,
        llm,
        embedder: Embedder,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self.embedder = embedder
        cfg.ensure_dirs()
        # v1.1 stored note paths relative to the data directory.  Move those
        # files into the vault once, without touching their text.
        self.legacy_notes_moved = notes_store.migrate_legacy_paths(self.db, self.cfg)
        self.prompts = PromptSet(prompt_loader.prompts_library(cfg))
        self.reasoner = Reasoner(llm, cfg.llm, prompts_set=self.prompts)
        self._import_items: dict[str, list] = {}

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _language(self, text: str, chat_id: str | int | None = None) -> str:
        """The reply language follows the current message, not a stored default."""

        detected = detect_language(text or "", "")
        if detected:
            return detected
        if chat_id is not None:
            previous = messages.last_user_language(self.db, chat_id, "")
            if previous:
                return previous
        return prefs.get_value(self.db, "language", self.cfg.default_language) or "en"

    def _detail(self) -> str:
        return prefs.get_value(self.db, "detail", "normal") or "normal"

    def _ru(self, language: str) -> bool:
        return (language or "").lower().startswith(("ru", "uk"))

    async def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not getattr(self.embedder, "enabled", False) or not texts:
            return []
        try:
            return await self.embedder.embed(list(texts))
        except (EmbeddingError, LLMError) as exc:
            log.warning("embedding failed: %s", exc)
            return []
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("embedding failed unexpectedly: %s", exc)
            return []

    async def _retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        include_sensitive: bool = False,
    ) -> list[SearchHit]:
        vectors = await self._embed([query])
        vector = vectors[0] if vectors else None
        return search.hybrid_search(
            self.db,
            query,
            query_vector=vector,
            embedding_model=getattr(self.embedder, "model", "") or None,
            top_k=top_k or self.cfg.search_top_k,
            include_sensitive=include_sensitive,
        )

    async def _embed_note(self, note: Note | None) -> None:
        if note is None or note.sensitive or not note.indexed:
            return
        vectors = await self._embed([search.embedding_text(note)])
        if vectors:
            search.save_embedding(
                self.db, note.id, self.embedder.model, vectors[0], at=now_iso()
            )

    def _wants_private(self, text: str) -> bool:
        lowered = text.lower()
        return any(
            word in lowered
            for word in ("private", "sensitive", "secret", "приват", "секрет", "чувствительн")
        )

    async def search_notes(
        self, query: str, *, limit: int = 8, include_sensitive: bool = False
    ) -> list[SearchHit]:
        """Public retrieval entry point used by the bot's /find command."""

        return await self._retrieve(query, top_k=limit, include_sensitive=include_sensitive)

    def _remember(
        self,
        chat_id: str,
        result: TurnResult,
        language: str,
        *,
        note_id: str | None = None,
    ) -> TurnResult:
        result.text = persona.finalize(result.text, data_dir=self.cfg.data_dir)
        project_id = None
        topic = ""
        if note_id or result.note_id:
            note = notes_store.get(self.db, note_id or result.note_id or "")
            if note is not None:
                project_id = note.project_id
                topic = note.type
        messages.add_message(
            self.db,
            chat_id=chat_id,
            role="assistant",
            text=result.text,
            language=language,
            project_id=project_id,
            topic=topic,
            note_id=note_id or result.note_id,
        )
        return result

    # ------------------------------------------------------------------
    # Main turn
    # ------------------------------------------------------------------

    async def handle_text(
        self,
        *,
        chat_id: str | int,
        text: str,
        message_id: int | None = None,
        source: str = "telegram",
        received_at: str | None = None,
        reply_to_raw: str | None = None,
        media: list[dict] | None = None,
    ) -> TurnResult:
        chat = str(chat_id)
        text = text or ""
        language = self._language(text, chat)

        external = self._sync_markdown()

        capture = raw.create(
            self.db,
            self.cfg,
            chat_id=chat,
            text=text,
            source=source,
            message_id=message_id,
            language=language,
            received_at=received_at,
            reply_to_id=reply_to_raw,
            media=media,
        )
        messages.add_message(
            self.db,
            chat_id=chat,
            role="user",
            text=text,
            language=language,
            telegram_message_id=message_id,
            raw_id=capture.id,
        )

        pending = messages.get_pending(self.db, chat)
        payload = (pending or {}).get("payload") or {}
        pending_kind = pending["kind"] if pending else None
        intent = intent_mod.classify(text, pending_kind=pending_kind)

        try:
            # Confirmations are handled before the general flow, but only when
            # the message really is a yes/no.  A control command keeps priority.
            if pending and pending["kind"] == "delete_confirm":
                handled = self._resolve_delete_confirmation(chat, text, language, payload, intent)
                if handled is not None:
                    return self._with_external_notice(handled, external, language, chat_id=chat)

            if pending and pending["kind"] == "onboarding_confirm":
                handled = self._onboarding_confirm_turn(chat, text, language, payload, intent)
                if handled is not None:
                    return self._with_external_notice(handled, external, language, chat_id=chat)

            if pending and pending["kind"] == "lesson_confirm" and intent.kind in {
                "affirm", "deny", "unknown", "answer"
            }:
                handled = self._resolve_lesson_confirmation(chat, text, language, payload)
                if handled is not None:
                    return self._with_external_notice(handled, external, language, chat_id=chat)

            # 1. Safety and destructive requests are refused, never executed.
            if intent.kind == "safety":
                result = self._safety_reply(language)
                return self._with_external_notice(
                    self._remember(chat, result, language), external, language, chat_id=chat
                )

            # 2. Flow control always beats onboarding, clarification, capture.
            if intent.kind == "flow_control":
                result = self._flow_control(chat, language, pending, intent)
                return self._with_external_notice(
                    self._remember(chat, result, language), external, language, chat_id=chat
                )

            # 3. A complaint is an explicit correction of the flow.  Apologise,
            # correct the state, never create a lesson, never continue.
            if intent.kind == "complaint":
                result = self._complaint_recovery(chat, language, pending)
                return self._with_external_notice(
                    self._remember(chat, result, language), external, language, chat_id=chat
                )

            # 4. Meta-questions about Alan are answered, not captured.
            if intent.kind in {"show_policy", "show_model"} or intent.action == "disclose":
                result = self._disclosure(chat, language, pending, intent)
                return self._with_external_notice(
                    self._remember(chat, result, language), external, language, chat_id=chat
                )

            # 5. Natural-language note controls.
            if intent.kind == "delete_last":
                result = self._delete_last_request(chat, language)
                return self._with_external_notice(
                    self._remember(chat, result, language), external, language, chat_id=chat
                )

            if intent.kind == "no_record":
                if pending and pending["kind"] == "clarify":
                    messages.clear_pending(self.db, chat)
                result = self._no_record_reply(language, pending)
                return self._with_external_notice(
                    self._remember(chat, result, language), external, language, chat_id=chat
                )

            # 6. Questions about the current conversation.
            if intent.kind == "memory":
                memory = self._memory_answer(chat, text, language)
                if memory is not None:
                    return self._with_external_notice(memory, external, language, chat_id=chat)

            # 7. Onboarding answers: only real answers are appended.
            if pending and pending["kind"] == "onboarding":
                if intent.kind in {"answer", "capture", "preference", "unknown"}:
                    result = self._onboarding_turn(chat, text, language, payload, raw_id=capture.id)
                    return self._with_external_notice(result, external, language, chat_id=chat)
                # A question or other non-answer is answered normally and the
                # onboarding state is preserved, never extended.

            if self._should_onboard(chat, text):
                reply = self.start_onboarding(chat, language)
                result = TurnResult(text=reply or "", intent="onboarding")
                return self._with_external_notice(
                    self._remember(chat, result, language), external, language, chat_id=chat
                )

            pending_questions: list[str] = []
            original_text: str | None = None
            original_raw: str | None = None
            if pending and pending["kind"] == "clarify":
                pending_questions = payload.get("questions") or []
                original_text = payload.get("original_text")
                original_raw = payload.get("raw_id")

            turn_text = text
            turn_capture = capture.id
            if intent.kind == "make_note":
                previous = messages.recent(self.db, chat, limit=2, role="user")
                if len(previous) < 2:
                    result = TurnResult(
                        text=(
                            "Нечего записывать, сэр: предыдущего сообщения нет."
                            if self._ru(language)
                            else "There is nothing to record, sir: no previous message."
                        ),
                        intent="make_note",
                    )
                    return self._with_external_notice(
                        self._remember(chat, result, language), external, language, chat_id=chat
                    )
                turn_text = previous[-2]["text"]
                turn_capture = previous[-2]["raw_id"] or capture.id

            result = await self._run_turn(
                chat_id=chat,
                text=turn_text,
                language=language,
                capture_id=turn_capture,
                pending_questions=pending_questions,
                original_text=original_text,
                original_raw=original_raw,
                preserve_pending=bool(pending and pending["kind"].startswith("onboarding")),
            )
            return self._with_external_notice(result, external, language, chat_id=chat)
        except (LLMError, ConfigError) as exc:
            log.exception("turn failed")
            result = TurnResult(text=safe_llm_error(exc), error=True)
            return self._remember(chat, result, language)
        except Exception:  # pragma: no cover - defensive
            log.exception("turn failed unexpectedly")
            result = TurnResult(text=persona.strings(language)["error"], error=True)
            return self._remember(chat, result, language)

    async def _run_turn(
        self,
        *,
        chat_id: str,
        text: str,
        language: str,
        capture_id: str,
        pending_questions: list[str],
        original_text: str | None,
        original_raw: str | None,
        preserve_pending: bool = False,
    ) -> TurnResult:
        combined_text = text
        if original_text and pending_questions:
            combined_text = f"{original_text}\n\nClarification answer: {text}"

        candidates = await self._retrieve(combined_text)

        assessment = await self.reasoner.assess(
            text=combined_text,
            language=language,
            now=utcnow().isoformat(timespec="minutes"),
            timezone=self.cfg.timezone,
            projects=projects_store.list_all(self.db, include_provisional=True),
            candidates=candidates,
            history=messages.history(self.db, chat_id, self.cfg.history_turns),
            pending_questions=pending_questions or None,
            original_text=original_text,
            preferences=prefs.prompt_block(self.db),
            lessons=lessons.prompt_block(self.db),
        )
        # The reply language is decided deterministically from the user's own
        # message (with the conversation as a fallback for very short replies).
        # A model that defaults to English must not be able to switch it.
        assessment.language = language

        if assessment.needs_clarification and assessment.clarifying_questions:
            payload = {
                "original_text": original_text or text,
                "questions": assessment.clarifying_questions[:2],
                "raw_id": original_raw or capture_id,
            }
            messages.set_pending(
                self.db,
                chat_id=chat_id,
                kind="clarify",
                payload=payload,
                expires_at=(utcnow() + timedelta(minutes=self.cfg.clarify_ttl_minutes)).isoformat(timespec="seconds"),
            )
            reply = persona.clarification(assessment.clarifying_questions, assessment.language)
            return self._remember(chat_id, TurnResult(text=reply, intent="clarify"), assessment.language)

        if not preserve_pending:
            messages.clear_pending(self.db, chat_id)

        if assessment.intent in {"question", "find", "overview"}:
            result = await self._answer_turn(
                chat_id=chat_id,
                text=text,
                assessment=assessment,
                candidates=candidates,
                language=assessment.language,
                capture_id=capture_id,
            )
            return self._remember(chat_id, result, assessment.language)

        if assessment.intent == "feedback":
            result = await self._feedback_turn(
                chat_id=chat_id,
                text=text,
                assessment=assessment,
                language=assessment.language,
                capture_id=capture_id,
            )
            return self._remember(chat_id, result, assessment.language)

        raw_ids = [capture_id] + ([original_raw] if original_raw else [])
        result = apply_mod.apply_assessment(
            self.db,
            self.cfg,
            assessment,
            raw_id=capture_id,
            raw_ids=raw_ids,
        )
        await self._embed_note(result.note)
        if result.note is not None:
            # Inbox-first: promote only a clear, typed, sourced note.
            result.note = notes_store.promote(self.db, self.cfg, result.note.id)

        if result.note is None:
            reply = self._fallback_reply(assessment, assessment.language, result.skipped_reason)
            return self._remember(
                chat_id, TurnResult(text=reply, intent=assessment.intent), assessment.language
            )

        related_titles = []
        for link in result.confirmed_links:
            target = notes_store.get(self.db, link.to_note_id)
            if target:
                related_titles.append(target.title)

        reply = persona.confirmation(
            result.note,
            created=result.created,
            language=assessment.language,
            project=result.project,
            project_is_new=result.project_is_new,
            related_titles=related_titles,
            raw_received_at=self._raw_time(result.note.id),
            open_questions=len(result.note.content.get("open_questions") or []),
        )

        if result.skipped_reason == "correction-target-missing":
            prefix = (
                "Не нашёл запись, которую вы имели в виду, сэр; сохранил это как новую заметку.\n\n"
                if self._ru(assessment.language)
                else "I could not find the record you meant, sir; recorded this as a new note.\n\n"
            )
            reply = prefix + reply

        buttons: list[list[Button]] = []
        if result.proposed_links:
            for link in result.proposed_links[:3]:
                target = notes_store.get(self.db, link.to_note_id)
                if target is None:
                    continue
                buttons.append([
                    Button(
                        label=("Связать: " if self._ru(assessment.language) else "Link: ") + target.title[:28],
                        data=f"link:confirm:{link.id}",
                    ),
                    Button(label="✕", data=f"link:reject:{link.id}"),
                ])
        if result.conflicts:
            reply += "\n\n" + self._conflict_warning(result.conflicts, assessment.language)
            buttons.append([Button(
                label=("Разобрать конфликт" if self._ru(assessment.language) else "Review conflict"),
                data=f"conflict:show:{result.conflicts[0].id}",
            )])
        if result.project_is_new and result.project is not None:
            buttons.append([
                Button(
                    label=("Подтвердить проект" if self._ru(assessment.language) else "Confirm project"),
                    data=f"project:confirm:{result.project.id}",
                ),
                Button(
                    label=("Переименовать" if self._ru(assessment.language) else "Rename"),
                    data=f"project:rename:{result.project.id}",
                ),
            ])

        return self._remember(
            chat_id,
            TurnResult(
                text=reply,
                buttons=buttons,
                note_id=result.note.id,
                intent=assessment.intent,
            ),
            assessment.language,
            note_id=result.note.id,
        )

    # ------------------------------------------------------------------
    # Intent-first controls and onboarding state machine
    # ------------------------------------------------------------------

    def _sir(self, language: str) -> str:
        return persona.strings(language)["sir"]

    def _onboarding_text(self, language: str, key: str, **values: object) -> str:
        suffix = "ru" if self._ru(language) else "en"
        return self.prompts.onboarding_section(f"{key} ({suffix})", **values)

    def _locality(self, provider: str, base_url: str = "") -> str:
        provider = (provider or "").lower()
        base = (base_url or "").lower()
        local = provider in {"local", "ollama", "lmstudio", "llamacpp", "llama.cpp"} or any(
            host in base for host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")
        )
        return local

    def _model_summary(self, language: str) -> str:
        ru = self._ru(language)
        model_local = self._locality(self.cfg.llm.provider, self.cfg.llm.base_url)
        emb_on = getattr(self.embedder, "enabled", False)
        emb_local = self._locality(self.cfg.embeddings.provider, self.cfg.embeddings.base_url)
        model_place = "локальная" if model_local else "облачная"
        emb_place = "локальные" if emb_local else "облачные"
        if ru:
            lines = [
                f"Модель рассуждений: {self.cfg.llm.provider}/{self.cfg.llm.model} ({model_place}).",
                (
                    f"Эмбеддинги: {self.cfg.embeddings.provider}/{self.cfg.embeddings.model} ({emb_place})."
                    if emb_on
                    else "Эмбеддинги: отключены; поиск работает по нормализованным ключевым словам."
                ),
                "Провайдер и модель заменяемы. От модели не зависят: хранилище и формат заметок, "
                "исходные захваты, память, приватность, правила интентов, конфликты и язык.",
            ]
            return "\n".join(lines)
        lines = [
            f"Reasoning model: {self.cfg.llm.provider}/{self.cfg.llm.model} ({'local' if model_local else 'cloud'}).",
            (
                f"Embeddings: {self.cfg.embeddings.provider}/{self.cfg.embeddings.model} "
                f"({'local' if emb_local else 'cloud'})."
                if emb_on
                else "Embeddings: disabled; retrieval uses normalised keywords."
            ),
            "Provider and model are replaceable. Model-independent: vault and note format, raw "
            "captures, memory, privacy, intent rules, conflicts, and language.",
        ]
        return "\n".join(lines)

    def _policy_reply(self, language: str) -> str:
        return persona.policy_text(
            language,
            model=self.cfg.llm.model,
            provider=self.cfg.llm.provider,
            embedding=(
                f"{self.cfg.embeddings.provider}/{self.cfg.embeddings.model}"
                if getattr(self.embedder, "enabled", False)
                else ""
            ),
            detail=self._detail(),
            memory_note=self._memory_note(),
            prompts_path=f"{self.cfg.vault_dir.name}/.alan/prompts",
            local=(
                "локальная" if self._ru(language) else "local"
            ) if self._locality(self.cfg.llm.provider, self.cfg.llm.base_url) else (
                "облачная" if self._ru(language) else "cloud"
            ),
        )

    def _disclosure(self, chat: str, language: str, pending: dict | None, intent) -> TurnResult:
        parts: list[str] = []
        if pending and str(pending.get("kind", "")).startswith("onboarding") and str(
            pending.get("kind")
        ) != "onboarding_paused":
            paused = self._pause_onboarding(chat, language, pending)
            parts.append(paused.text)
        wants_policy = intent.kind == "show_policy" or "show_policy" in intent.secondary
        wants_model = intent.kind == "show_model" or "show_model" in intent.secondary
        if wants_policy:
            parts.append(self._policy_reply(language))
        if wants_model:
            parts.append(self._model_summary(language))
        if not parts:
            parts.append(self._policy_reply(language))
        return TurnResult(text="\n\n".join(parts), intent="self_disclosure")

    def _safety_reply(self, language: str) -> TurnResult:
        body = (
            "Не выполняю массовое удаление, сэр. Исходные записи неизменяемы, а заметки удаляются "
            "только по одной и после подтверждения. Скажите, что именно удалить."
            if self._ru(language)
            else "I do not perform bulk deletion, sir. Raw captures are immutable, and notes are "
            "deleted one at a time after confirmation. Tell me exactly what to remove."
        )
        return TurnResult(text=body, intent="safety")

    # -- public self-disclosure helpers used by the bot ------------------

    def policy_reply(self, language: str | None = None) -> str:
        return self._policy_reply(language or self._preferred_language())

    def model_summary(self, language: str | None = None) -> str:
        return self._model_summary(language or self._preferred_language())

    def reload_prompts(self) -> TurnResult:
        prompt_loader.reload_prompts(self.cfg)
        self.prompts = PromptSet(prompt_loader.prompts_library(self.cfg))
        self.reasoner.prompts = self.prompts
        language = self._preferred_language()
        path = f"{self.cfg.vault_dir.name}/.alan/prompts"
        body = (
            f"Перечитал промпты из `{path}/`, сэр. Изменения применены."
            if self._ru(language)
            else f"Reloaded the prompts from `{path}/`, sir. The changes are applied."
        )
        return TurnResult(text=body, intent="self_disclosure")

    def _flow_control(self, chat: str, language: str, pending: dict | None, intent) -> TurnResult:
        if intent.action == "resume":
            return self._resume_onboarding(chat, language, pending)
        result = self._pause_onboarding(chat, language, pending)
        extra: list[str] = []
        if "show_policy" in intent.secondary:
            extra.append(self._policy_reply(language))
        if "show_model" in intent.secondary:
            extra.append(self._model_summary(language))
        if extra:
            result.text = "\n\n".join([result.text, *extra])
        return result

    def _pause_onboarding(self, chat: str, language: str, pending: dict | None) -> TurnResult:
        kind = str((pending or {}).get("kind") or "")
        if kind.startswith("onboarding"):
            payload = dict((pending or {}).get("payload") or {})
        elif kind == "clarify":
            messages.clear_pending(self.db, chat)
            body = (
                "Остановил уточнение, сэр. Исходное сообщение сохранено без изменений."
                if self._ru(language)
                else "I stopped the clarification, sir. The original message is preserved unchanged."
            )
            return TurnResult(text=body, intent="flow_control")
        elif kind in {"delete_confirm", "lesson_confirm"}:
            messages.clear_pending(self.db, chat)
            if kind == "delete_confirm":
                body = (
                    "Отменил удаление, сэр. Ничего не удалено."
                    if self._ru(language)
                    else "Deletion cancelled, sir. Nothing was removed."
                )
            else:
                body = (
                    "Отменил, сэр. Урок не создан."
                    if self._ru(language)
                    else "Cancelled, sir. No lesson was created."
                )
            return TurnResult(text=body, intent="flow_control")
        else:
            body = (
                "Сейчас нет активного процесса, который можно остановить, сэр."
                if self._ru(language)
                else "There is no active workflow to stop, sir."
            )
            return TurnResult(text=body, intent="flow_control")
        payload["language"] = language
        payload["paused"] = True
        messages.set_pending(
            self.db,
            chat_id=chat,
            kind="onboarding_paused",
            payload=payload,
            expires_at=(utcnow() + timedelta(days=30)).isoformat(timespec="seconds"),
        )
        body = self._onboarding_text(
            language,
            "Pause",
            sir=self._sir(language),
            resume_command=("продолжи настройку" if self._ru(language) else "continue setup"),
        )
        return TurnResult(text=body, intent="onboarding")

    def _resume_onboarding(self, chat: str, language: str, pending: dict | None) -> TurnResult:
        kind = str((pending or {}).get("kind") or "")
        payload = dict((pending or {}).get("payload") or {})
        if kind == "onboarding_confirm":
            return TurnResult(
                text=self._onboarding_summary(payload.get("answers") or [], language),
                intent="onboarding",
            )
        if kind in {"onboarding", "onboarding_paused"}:
            questions = self.prompts.onboarding_questions(language)
            step = max(0, min(int(payload.get("step") or 0), len(questions) - 1))
            payload["language"] = language
            payload.pop("paused", None)
            messages.set_pending(
                self.db,
                chat_id=chat,
                kind="onboarding",
                payload=payload,
                expires_at=(utcnow() + timedelta(days=7)).isoformat(timespec="seconds"),
            )
            if not questions:
                return TurnResult(text=self._policy_reply(language), intent="onboarding")
            head = self._onboarding_text(
                language,
                "Resume",
                sir=self._sir(language),
                step=step + 1,
                total=len(questions),
            )
            return TurnResult(text=f"{head}\n\n{questions[step]}", intent="onboarding")
        if self.onboarding_done():
            body = (
                "Настройка уже завершена, сэр. Профиль сохранён."
                if self._ru(language)
                else "The setup is already complete, sir. The profile is saved."
            )
            return TurnResult(text=body, intent="onboarding")
        if not self.cfg.onboarding_enabled:
            body = (
                "Настройка отключена в конфигурации, сэр."
                if self._ru(language)
                else "Setup is disabled in the configuration, sir."
            )
            return TurnResult(text=body, intent="onboarding")
        return TurnResult(text=self.start_onboarding(chat, language) or "", intent="onboarding")

    def _complaint_recovery(self, chat: str, language: str, pending: dict | None) -> TurnResult:
        """Acknowledge the mistake, correct the state, and stop the flow.

        No lesson is created.  A profile that was built from non-answers is
        marked ``needs-clarification`` and moved back to the Inbox.
        """

        profile = notes_store.find(self.db, "user-profile")
        marked = ""
        if profile is not None:
            notes_store.set_status(self.db, profile.id, "needs-clarification")
            notes_store.promote(self.db, self.cfg, profile.id)
            marked = profile.title
        kind = str((pending or {}).get("kind") or "")
        if kind.startswith("onboarding"):
            payload = dict((pending or {}).get("payload") or {})
            payload["answers"] = []
            payload["step"] = 0
            payload["language"] = language
            payload["paused"] = True
            messages.set_pending(
                self.db,
                chat_id=chat,
                kind="onboarding_paused",
                payload=payload,
                expires_at=(utcnow() + timedelta(days=30)).isoformat(timespec="seconds"),
            )
        body = self._onboarding_text(language, "Complaint", sir=self._sir(language))
        if marked:
            body += (
                f"\n\nЗапись «{marked}» помечена как требующая уточнения и возвращена в Inbox."
                if self._ru(language)
                else f"\n\nThe entry “{marked}” is marked as needing clarification and moved back to the Inbox."
            )
        return TurnResult(text=body, intent="correction")

    def _delete_last_request(self, chat: str, language: str) -> TurnResult:
        notes = notes_store.list_notes(self.db, limit=1, include_sensitive=True)
        if not notes:
            body = (
                "В базе нет заметок для удаления, сэр."
                if self._ru(language)
                else "There are no notes to delete, sir."
            )
            return TurnResult(text=body, intent="delete_last")
        note = notes[0]
        messages.set_pending(
            self.db,
            chat_id=chat,
            kind="delete_confirm",
            payload={"note_id": note.id, "title": note.title, "language": language},
            expires_at=(utcnow() + timedelta(minutes=self.cfg.clarify_ttl_minutes)).isoformat(timespec="seconds"),
        )
        body = (
            f"Подтвердите, сэр: удалить последнюю заметку «{note.title}»? "
            "Исходное сообщение останется в защищённом хранилище."
            if self._ru(language)
            else f"Confirm, sir: delete the last note “{note.title}”? "
            "The raw capture remains in the protected store."
        )
        return TurnResult(text=body, intent="delete_last")

    def _resolve_delete_confirmation(
        self, chat: str, text: str, language: str, payload: dict, intent
    ) -> TurnResult | None:
        if intent.kind == "affirm":
            messages.clear_pending(self.db, chat)
            return self.delete_note(str(payload.get("note_id") or ""), language)
        if intent.kind == "deny":
            messages.clear_pending(self.db, chat)
            body = "Не удаляю, сэр." if self._ru(language) else "I will not delete it, sir."
            return TurnResult(text=body, intent="delete_last")
        messages.clear_pending(self.db, chat)
        return None

    def _no_record_reply(self, language: str, pending: dict | None) -> TurnResult:
        onboarding = bool(pending and str(pending.get("kind", "")).startswith("onboarding"))
        if self._ru(language):
            body = "Понял, сэр. Не записываю это как заметку."
            if onboarding:
                body += " И не считаю это ответом на вопрос настройки."
        else:
            body = "Understood, sir. I will not record this as a note."
            if onboarding:
                body += " I am also not treating it as an answer to the setup question."
        return TurnResult(text=body, intent="no_record")

    def _onboarding_summary(self, answers: list[str], language: str) -> str:
        directions = "; ".join(one_line(a)[:160] for a in answers if str(a).strip()) or (
            "пока не указаны" if self._ru(language) else "not provided yet"
        )
        return self._onboarding_text(
            language,
            "Summary",
            sir=self._sir(language),
            directions=directions[:600],
            sources=("пока не выбраны" if self._ru(language) else "not selected yet"),
        )

    def _onboarding_confirm_turn(
        self, chat: str, text: str, language: str, payload: dict, intent
    ) -> TurnResult | None:
        answers = [str(a) for a in (payload.get("answers") or [])]
        if intent.kind == "affirm":
            messages.clear_pending(self.db, chat)
            body = self._complete_onboarding(
                chat,
                answers,
                language,
                raw_id=payload.get("raw_id"),
                raw_ids=payload.get("raw_ids") or [],
            )
            return self._remember(chat, TurnResult(text=body, intent="onboarding"), language)
        if intent.kind == "deny":
            messages.set_pending(
                self.db,
                chat_id=chat,
                kind="onboarding_paused",
                payload={**payload, "answers": answers, "paused": True, "language": language},
                expires_at=(utcnow() + timedelta(days=30)).isoformat(timespec="seconds"),
            )
            body = self._onboarding_text(
                language,
                "Declined",
                sir=self._sir(language),
                resume_command=("продолжи настройку" if self._ru(language) else "continue setup"),
            )
            return self._remember(chat, TurnResult(text=body, intent="onboarding"), language)
        if intent.kind in {
            "flow_control", "complaint", "show_policy", "show_model",
            "memory", "no_record", "safety", "delete_last", "make_note",
        }:
            return None
        # Any other message re-asks for confirmation without losing the answers.
        return self._remember(
            chat,
            TurnResult(
                text=self._onboarding_summary(answers, language),
                intent="onboarding",
            ),
            language,
        )

    def _raw_time(self, note_id: str) -> str | None:
        captures = raw.for_note(self.db, note_id)
        return captures[0].received_at if captures else None

    def _fallback_reply(self, assessment: Assessment, language: str, skipped: str = "") -> str:
        ru = self._ru(language)
        if assessment.reply:
            return persona.ensure_address(assessment.reply, language)
        if skipped == "correction-target-missing":
            if ru:
                return "Не понял, какую запись исправить, сэр. Укажите её — и я внесу правку."
            return "I could not identify which record to correct, sir. Which note do you mean?"
        if skipped == "clarification":
            if ru:
                return "Нужно уточнение, сэр. Напишите ответ одним сообщением."
            return "I need one clarification, sir. Send it in a single message."
        if ru:
            return (
                "Сообщение сохранено как исходное, сэр, но устойчивой заметки из него не выходит. "
                "Скажите, что именно стоит запомнить."
            )
        return (
            "The message is preserved as a raw capture, sir, but I could not form a durable note from it. "
            "Tell me what should be remembered."
        )

    def _conflict_warning(self, found: Sequence[object], language: str) -> str:
        n = len(found)
        if self._ru(language):
            return f"Обнаружен конфликт с существующей записью (~{n}). Пока не решено, что верно."
        return f"Conflict detected with an existing record ({n}). It stays unresolved until you decide."

    # ------------------------------------------------------------------
    # Working memory: deterministic answers about this conversation
    # ------------------------------------------------------------------

    def _memory_answer(self, chat_id: str, text: str, language: str) -> TurnResult | None:
        lowered = (text or "").lower().strip()
        if not any(trigger in lowered for trigger in _MEMORY_TRIGGERS):
            return None

        ru = self._ru(language)
        prior = messages.recent(self.db, chat_id, limit=8, role="user")
        prior = prior[:-1] if prior else []
        previous = prior[-1]["text"] if prior else None
        last_reply = messages.recent(self.db, chat_id, limit=1, role="assistant")
        last_reply_text = last_reply[-1]["text"] if last_reply else None

        wants_explain = (
            ("объясни" in lowered and ("ответ" in lowered or "сказал" in lowered))
            or "explain your" in lowered
        )
        wants_previous_question = (
            ("вопрос" in lowered and ("прошл" in lowered or "предыдущ" in lowered))
            or "previous question" in lowered
            or "last question" in lowered
        )
        wants_remember = "помнишь" in lowered or "remember" in lowered
        wants_previous = (
            ("сообщени" in lowered and ("прошл" in lowered or "предыдущ" in lowered))
            or "previous message" in lowered
            or "last message" in lowered
            or "имел в виду" in lowered
            or "meant the" in lowered
            or "what did i" in lowered
        )

        if wants_explain:
            if not last_reply_text:
                body = persona.strings(language)["nothing_remembered"].format(sir=persona.strings(language)["sir"])
            else:
                quote = one_line(last_reply_text)[:400]
                body = (
                    f"Мой предыдущий ответ: «{quote}»."
                    if ru else f"My previous answer: “{quote}”."
                )
            return self._remember(chat_id, TurnResult(text=body, intent="memory"), language)

        if wants_previous_question:
            question = next(
                (row["text"] for row in reversed(prior) if "?" in (row["text"] or "")), previous
            )
            if not question:
                body = persona.strings(language)["nothing_remembered"].format(sir=persona.strings(language)["sir"])
            else:
                body = (
                    f"Ваш предыдущий вопрос: «{one_line(question)[:400]}»."
                    if ru else f"Your previous question: “{one_line(question)[:400]}”."
                )
            return self._remember(chat_id, TurnResult(text=body, intent="memory"), language)

        if wants_previous:
            if not previous:
                body = persona.strings(language)["nothing_remembered"].format(sir=persona.strings(language)["sir"])
            else:
                body = (
                    f"Ваше предыдущее сообщение: «{one_line(previous)[:400]}»."
                    if ru else f"Your previous message: “{one_line(previous)[:400]}”."
                )
                if last_reply_text and ("имел в виду" in lowered or "meant the" in lowered):
                    body += (
                        "\nУточните, что именно поправить в предыдущем ответе, и я обновлю запись."
                        if ru else "\nTell me what to correct in that answer and I will update the record."
                    )
            return self._remember(chat_id, TurnResult(text=body, intent="memory"), language)

        if wants_remember:
            user_turns = max(0, messages.count(self.db, chat_id, role="user") - 1)
            stats = notes_store.stats(self.db)
            pending = messages.get_pending(self.db, chat_id)
            if ru:
                parts = [
                    f"В текущем разговоре я помню {user_turns} предыдущих сообщений.",
                    "В базе знаний сейчас "
                    f"{stats['notes']} заметок и {stats['raw']} исходных записей.",
                ]
                if pending:
                    parts.append("Есть незавершённое уточнение — я жду вашего ответа.")
                recent_users = [one_line(row["text"])[:160] for row in prior[-3:] if row["text"]]
                if recent_users:
                    parts.append("Последние ваши сообщения: " + "; ".join(f"«{u}»" for u in recent_users) + ".")
                if last_reply_text:
                    parts.append(f"Мой последний ответ: «{one_line(last_reply_text)[:240]}».")
            else:
                parts = [
                    f"In this conversation I remember {user_turns} previous messages.",
                    f"The knowledge base holds {stats['notes']} notes and {stats['raw']} raw captures.",
                ]
                if pending:
                    parts.append("There is an unresolved clarification waiting for your answer.")
                recent_users = [one_line(row["text"])[:160] for row in prior[-3:] if row["text"]]
                if recent_users:
                    parts.append("Your recent messages: " + "; ".join(f"“{u}”" for u in recent_users) + ".")
                if last_reply_text:
                    parts.append(f"My last answer: “{one_line(last_reply_text)[:240]}”.")
            return self._remember(
                chat_id, TurnResult(text=" ".join(parts), intent="memory"), language
            )

        return None

    def _memory_note(self) -> str:
        stats = notes_store.stats(self.db)
        language = self._preferred_language()
        if self._ru(language):
            return f"{stats['notes']} заметок, {lessons.count_active(self.db)} активных уроков"
        return f"{stats['notes']} notes, {lessons.count_active(self.db)} active lessons"

    # ------------------------------------------------------------------
    # Markdown external edits
    # ------------------------------------------------------------------

    def _sync_markdown(self) -> list[str]:
        if not self.cfg.markdown_watch:
            return []
        try:
            return notes_store.sync_external_edits(self.db, self.cfg, limit=1000)
        except Exception:  # pragma: no cover - a bad file must not break a turn
            log.exception("markdown sync failed")
            return []

    def sync_markdown(self) -> list[str]:
        """Public entry point used at startup and by the test suite."""

        return self._sync_markdown()

    def refresh_metadata(self, note_id: str) -> TurnResult:
        note = notes_store.get(self.db, note_id)
        if note is None:
            return TurnResult(text="No such note, sir.", error=True)
        notes_store.refresh_metadata(self.db, self.cfg, note_id)
        language = self._preferred_language()
        return TurnResult(text=persona.strings(language)["metadata_refreshed"].format(sir=persona.strings(language)["sir"]))

    def keep_metadata(self, note_id: str) -> TurnResult:
        language = self._preferred_language()
        return TurnResult(text=persona.strings(language)["metadata_kept"].format(sir=persona.strings(language)["sir"]))

    def _with_external_notice(
        self, result: TurnResult, changed: list[str], language: str, *, chat_id: str = ""
    ) -> TurnResult:
        if not changed:
            return result
        note = notes_store.get(self.db, changed[0])
        if note is None:
            return result
        notice = persona.external_edit_notice(language, title=note.title, version=note.version)
        result.text = (notice + "\n\n" + (result.text or "")).strip()
        if chat_id:
            messages.prepend_to_last_assistant(self.db, chat_id, notice)
        result.buttons.insert(0, [
            Button(
                label=("Обновить метаданные" if self._ru(language) else "Update metadata"),
                data=f"metadata:refresh:{note.id}",
            ),
            Button(
                label=("Оставить как есть" if self._ru(language) else "Leave as-is"),
                data=f"metadata:keep:{note.id}",
            ),
        ])
        return result

    # ------------------------------------------------------------------
    # Answers
    # ------------------------------------------------------------------

    async def _answer_turn(
        self,
        *,
        chat_id: str,
        text: str,
        assessment: Assessment,
        candidates: Sequence[SearchHit],
        language: str,
        capture_id: str,
    ) -> TurnResult:
        query = assessment.search_query.strip() or text
        include_sensitive = self._wants_private(text)
        hits = await self._retrieve(
            query, top_k=self.cfg.answer_context_k, include_sensitive=include_sensitive
        )

        notes: list[Note] = []
        seen: set[str] = set()
        for hit in list(hits) + list(candidates):
            if hit.note.id not in seen:
                seen.add(hit.note.id)
                notes.append(hit.note)

        if not notes:
            return TurnResult(text=persona.nothing_found(language), intent=assessment.intent)

        found_conflicts: list[dict[str, object]] = []
        for note in notes:
            for conflict in conflicts.for_note(self.db, note.id):
                other = notes_store.get(
                    self.db,
                    conflict.other_note_id if conflict.note_id == note.id else conflict.note_id,
                )
                found_conflicts.append(
                    {
                        "note_id": note.id,
                        "note_title": note.title,
                        "other_note_id": other.id if other else "",
                        "other_title": other.title if other else "(unknown)",
                        "existing_claim": conflict.existing_claim,
                        "new_claim": conflict.new_claim,
                        "explanation": conflict.explanation,
                    }
                )

        draft = await self.reasoner.answer(
            question=text,
            notes=notes[: self.cfg.answer_context_k],
            conflicts=found_conflicts,
            history=messages.history(self.db, chat_id, self.cfg.history_turns),
            lessons=lessons.prompt_block(self.db),
            preferences=prefs.prompt_block(self.db),
            detail=self._detail(),
            language=language,
        )

        body = persona.ensure_address(draft.answer.strip(), language)
        if not body:
            body = persona.strings(language)["no_answer"].format(sir=persona.strings(language)["sir"])

        source_notes = [note for note in notes if note.id in set(draft.sources)]
        if source_notes:
            body += "\n\n" + persona.sources_block(source_notes, language)
        elif notes and draft.evidence:
            body += "\n\n" + persona.sources_block([n for n in notes[:2]], language)

        if draft.unresolved:
            label = "Неизвестно" if self._ru(language) else "Unknown"
            body += f"\n{label}: " + "; ".join(draft.unresolved[:3])

        if found_conflicts and not draft.sources:
            body += "\n\n" + self._conflict_warning(found_conflicts, language)

        buttons: list[list[Button]] = []
        for note in source_notes[:3]:
            buttons.append([Button(label=f"↗ {note.title[:40]}", data=f"note:show:{note.id}")])

        return TurnResult(text=body, intent=assessment.intent, buttons=buttons)

    # ------------------------------------------------------------------
    # Feedback, lessons, preferences
    # ------------------------------------------------------------------

    async def _feedback_turn(
        self,
        *,
        chat_id: str,
        text: str,
        assessment: Assessment,
        language: str,
        capture_id: str | None = None,
    ) -> TurnResult:
        ru = self._ru(language)
        s = persona.strings(language)
        candidate = assessment.lesson
        preference = assessment.preference
        durable = assessment.durable_intent or "none"
        lesson = None

        preference_applied = ""
        if preference is not None and preference.key and preference.value and durable == "durable":
            prefs.set_value(
                self.db,
                preference.key,
                preference.value,
                source="conversation",
                actor="alan",
            )
            preference_applied = s["preference_set"].format(
                sir=s["sir"], key=preference.key, value=preference.value
            )

        if candidate is not None and candidate.text:
            if durable == "durable":
                lesson = lessons.add(
                    self.db,
                    kind=candidate.kind or "preference",
                    text=candidate.text,
                    scope=candidate.scope or "global",
                    evidence_raw_id=capture_id,
                    confidence=candidate.confidence,
                    explanation=candidate.explanation,
                    model=self.cfg.llm.model,
                    confirmed=True,
                )
            elif durable == "unclear":
                messages.set_pending(
                    self.db,
                    chat_id=chat_id,
                    kind="lesson_confirm",
                    payload={
                        "text": candidate.text,
                        "kind": candidate.kind or "preference",
                        "scope": candidate.scope or "global",
                        "confidence": candidate.confidence,
                        "explanation": candidate.explanation,
                        "raw_id": capture_id,
                        "preference": (
                            {"key": preference.key, "value": preference.value}
                            if preference is not None else None
                        ),
                    },
                    expires_at=(utcnow() + timedelta(minutes=self.cfg.clarify_ttl_minutes)).isoformat(timespec="seconds"),
                )
                prefix = persona.ensure_address(assessment.reply, language) if assessment.reply else ""
                question = persona.lesson_question(language)
                body = f"{prefix}\n\n{question}".strip() if prefix else question
                return TurnResult(text=body, intent="feedback")
            elif durable == "one_time":
                return TurnResult(text=s["lesson_declined"].format(sir=s["sir"]), intent="feedback")
            else:
                # "none" plus an explicit candidate is a contradiction in the
                # model output; ask rather than silently creating a rule.
                prefix = persona.ensure_address(assessment.reply, language) if assessment.reply else ""
                question = persona.lesson_question(language)
                body = f"{prefix}\n\n{question}".strip() if prefix else question
                return TurnResult(text=body, intent="feedback")

        elif (
            assessment.correction is not None
            and assessment.correction.lesson
            and durable != "unclear"
        ):
            # Backwards-compatible path: corrections may still carry a lesson.
            lesson = lessons.add(
                self.db,
                kind=assessment.correction.lesson_kind or "interpretation",
                text=assessment.correction.lesson,
                confidence=0.7,
                explanation=assessment.reply or "",
                model=self.cfg.llm.model,
            )
            body = (
                f"Принято, сэр.\nУрок: {lesson.text}"
                if ru else f"Noted, sir.\nLesson: {lesson.text}"
            )
            return TurnResult(text=body, intent="feedback")

        if lesson is not None:
            body = persona.lesson_created(language, lesson_id=lesson.id, text=lesson.text)
            if preference_applied:
                body = preference_applied + "\n" + body
            return TurnResult(text=body, intent="feedback")

        if preference_applied:
            return TurnResult(text=preference_applied, intent="feedback")

        if assessment.reply:
            return TurnResult(
                text=persona.ensure_address(assessment.reply, language), intent="feedback"
            )

        body = "Принял, сэр. Учту это в дальнейшем." if ru else "Noted, sir. I will take that into account."
        return TurnResult(text=body, intent="feedback")

    def _resolve_lesson_confirmation(
        self, chat_id: str, text: str, language: str, payload: dict
    ) -> TurnResult | None:
        s = persona.strings(language)
        if _YES.match(text or ""):
            lesson = lessons.add(
                self.db,
                kind=str(payload.get("kind") or "preference"),
                text=str(payload.get("text") or ""),
                scope=str(payload.get("scope") or "global"),
                evidence_raw_id=payload.get("raw_id"),
                confidence=float(payload.get("confidence") or 0.6),
                explanation=str(payload.get("explanation") or ""),
                model=self.cfg.llm.model,
                confirmed=True,
            )
            preference = payload.get("preference") or {}
            if preference.get("key") and preference.get("value"):
                prefs.set_value(
                    self.db, preference["key"], preference["value"], source="conversation", actor="alan"
                )
            messages.clear_pending(self.db, chat_id)
            body = persona.lesson_created(language, lesson_id=lesson.id, text=lesson.text)
            return self._remember(chat_id, TurnResult(text=body, intent="feedback"), language)
        if _NO.match(text or ""):
            messages.clear_pending(self.db, chat_id)
            body = s["lesson_declined"].format(sir=s["sir"])
            return self._remember(chat_id, TurnResult(text=body, intent="feedback"), language)
        messages.clear_pending(self.db, chat_id)
        return None

    async def reflect(self, chat_id: str | int) -> str:
        transcript = "\n".join(
            f"{turn['role']}: {turn['text']}"
            for turn in messages.history(self.db, chat_id, 20)
        )
        if not transcript.strip():
            return "Nothing to reflect on, sir."
        draft = await self.reasoner.reflect(transcript=transcript)
        added = []
        for item in draft.lessons[:3]:
            if item.text:
                added.append(
                    lessons.add(
                        self.db,
                        kind=item.kind or "interpretation",
                        text=item.text,
                        scope=item.scope or "global",
                        confidence=item.confidence,
                        explanation=item.explanation,
                        model=self.cfg.llm.model,
                        confirmed=False,
                    )
                )
        if not added:
            return "No durable lesson in the recent exchange, sir."
        lines = ["Recorded, sir. Lessons:"] + [f"- {lesson.text}" for lesson in added]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Actions from inline keyboards
    # ------------------------------------------------------------------

    def confirm_link(self, link_id: str) -> TurnResult:
        link = links.get(self.db, link_id)
        if link is None:
            return TurnResult(text="That link no longer exists, sir.", error=True)
        links.set_status(self.db, link_id, "confirmed")
        notes_store.rerender(self.db, self.cfg, link.from_note_id)
        notes_store.rerender(self.db, self.cfg, link.to_note_id)
        return TurnResult(text="Linked, sir.")

    def reject_link(self, link_id: str) -> TurnResult:
        links.set_status(self.db, link_id, "rejected")
        return TurnResult(text="Noted, sir. No link created.")

    def show_conflict(self, conflict_id: str) -> TurnResult:
        conflict = conflicts.get(self.db, conflict_id)
        if conflict is None:
            return TurnResult(text="That conflict no longer exists, sir.", error=True)
        new_note = notes_store.get(self.db, conflict.note_id)
        old_note = notes_store.get(self.db, conflict.other_note_id)
        text = (
            "Conflict detected, sir.\n\n"
            f"{old_note.title if old_note else '(unknown)'}: “{conflict.existing_claim}”\n"
            f"{new_note.title if new_note else '(unknown)'}: “{conflict.new_claim}”\n\n"
            f"{conflict.explanation}\n\nWhich reflects your current position?"
        )
        buttons = [
            [
                Button(label="Newer record is current", data=f"conflict:new:{conflict_id}"),
                Button(label="Earlier record is current", data=f"conflict:old:{conflict_id}"),
            ],
            [
                Button(label="Both hold in different contexts", data=f"conflict:both:{conflict_id}"),
                Button(label="Leave unresolved", data=f"conflict:keep:{conflict_id}"),
            ],
        ]
        return TurnResult(text=text, buttons=buttons)

    def resolve_conflict(self, conflict_id: str, winner: str, language: str = "en") -> TurnResult:
        conflict = conflicts.get(self.db, conflict_id)
        if conflict is None:
            return TurnResult(text="That conflict no longer exists, sir.", error=True)
        if winner == "keep":
            return TurnResult(text="Left unresolved, sir. It will remain visible in search.")
        resolution = {
            "new": "The newer record reflects the user's current position.",
            "old": "The earlier record reflects the user's current position.",
            "both": "Both records hold in different contexts.",
        }.get(winner, "Resolved by the user.")
        conflicts.resolve(self.db, conflict_id, resolution=resolution, winner=None if winner == "both" else winner)
        for nid in (conflict.note_id, conflict.other_note_id):
            note = notes_store.get(self.db, nid)
            if note:
                notes_store.rerender(self.db, self.cfg, note.id)
        return TurnResult(text="Resolved, sir. The affected notes are updated and the superseded version is preserved.")

    def confirm_project(self, project_id: str, name: str | None = None) -> TurnResult:
        project = projects_store.confirm(self.db, project_id, name=name)
        if project is None:
            return TurnResult(text="That project no longer exists, sir.", error=True)
        return TurnResult(text=f"Project confirmed, sir: {project.name}.")

    # ------------------------------------------------------------------
    # Privacy and deletion
    # ------------------------------------------------------------------

    def set_note_private(self, note_id: str, sensitive: bool) -> TurnResult:
        note = notes_store.get(self.db, note_id)
        if note is None:
            return TurnResult(text="No such note, sir.", error=True)
        notes_store.set_private(self.db, note_id, sensitive)
        if sensitive:
            return TurnResult(text="Marked private, sir. Excluded from search, summaries and proactive messages.")
        return TurnResult(text="No longer private, sir. Re-indexed.")

    def delete_note(self, note_id: str, language: str | None = None) -> TurnResult:
        language = language or self._preferred_language()
        note = notes_store.get(self.db, note_id)
        if note is None:
            body = "Такой заметки нет, сэр." if self._ru(language) else "No such note, sir."
            return TurnResult(text=body, error=True)
        notes_store.delete(self.db, self.cfg, note_id)
        body = (
            f"Удалено, сэр: {note.title}. Исходное сообщение останется, если не удалить его отдельно."
            if self._ru(language)
            else f"Deleted, sir: {note.title}. The raw capture remains unless you delete it explicitly."
        )
        return TurnResult(text=body)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_archive(self) -> Path:
        return export_store.export_all(self.db, self.cfg)

    # ------------------------------------------------------------------
    # Proactive
    # ------------------------------------------------------------------

    async def proactive_check(self, chat_id: str | int) -> TurnResult | None:
        from ..proactive import review

        if not self.cfg.proactive.enabled:
            return None
        if prefs.get_value(self.db, "proactive", "on") == "off":
            return None
        if not review.within_send_window(self.db, self.cfg, chat_id):
            return None

        candidates = review.collect_candidates(self.db, self.cfg, chat_id)
        if not candidates:
            return None

        draft = await self.reasoner.proactive(
            now=utcnow().isoformat(timespec="minutes"),
            candidates=review.render_candidates(candidates),
            recent=review.render_recent(self.db, chat_id),
            last_sent=reminders.last_proactive_at(self.db, chat_id),
        )
        if not draft.worth_sending or not draft.message.strip():
            review.log_skipped(self.db, chat_id, candidates)
            return None

        body = persona.ensure_address(draft.message.strip(), self._preferred_language())
        reminders.log_proactive(
            self.db,
            chat_id=chat_id,
            kinds=draft.kinds or [c.kind for c in candidates],
            note_ids=draft.note_ids or [c.note_id for c in candidates if c.note_id],
        )
        for candidate in candidates:
            if candidate.reminder_id:
                reminders.mark_sent(self.db, candidate.reminder_id)
        messages.add_message(self.db, chat_id=chat_id, role="assistant", text=body, language=self._preferred_language())
        return TurnResult(text=body, intent="proactive")

    def _preferred_language(self) -> str:
        return prefs.get_value(self.db, "language", self.cfg.default_language) or "en"

    # ------------------------------------------------------------------
    # Onboarding
    # ------------------------------------------------------------------

    def onboarding_done(self) -> bool:
        return prefs.get_value(self.db, "onboarding_done") == "1"

    def _should_onboard(self, chat_id: str, text: str) -> bool:
        if not self.cfg.onboarding_enabled or self.onboarding_done():
            return False
        if messages.count(self.db, chat_id, role="user") > 1:
            return False
        lowered = (text or "").strip().lower().strip("!., ")
        return lowered in {
            "привет", "здравствуйте", "добрый день", "добрый вечер", "hello", "hi", "hey", "start",
        }

    def start_onboarding(self, chat_id: str | int, language: str | None = None) -> str | None:
        if not self.cfg.onboarding_enabled:
            return None
        language = language or self._preferred_language()
        if self.onboarding_done():
            return None
        questions = self.prompts.onboarding_questions(language)
        messages.set_pending(
            self.db,
            chat_id=chat_id,
            kind="onboarding",
            payload={"step": 0, "answers": [], "raw_ids": [], "language": language},
            expires_at=(utcnow() + timedelta(days=7)).isoformat(timespec="seconds"),
        )
        intro = self._onboarding_text(language, "Intro", sir=self._sir(language))
        if not questions:
            return intro
        return f"{intro}\n\n{questions[0]}"

    def _onboarding_turn(
        self, chat_id: str, text: str, language: str, payload: dict, *, raw_id: str | None = None
    ) -> TurnResult:
        """Record one real answer and ask the next question.

        Control commands, complaints, and meta-questions never reach this
        method: they are handled first and never appended to ``answers``.
        """

        answers = list(payload.get("answers") or [])
        raw_ids = list(payload.get("raw_ids") or [])
        step = int(payload.get("step") or 0)
        questions = self.prompts.onboarding_questions(language)
        if step >= len(questions):
            questions = []

        answers.append(text)
        if raw_id and raw_id not in raw_ids:
            raw_ids.append(raw_id)
        step += 1
        payload.update(
            {"answers": answers, "raw_ids": raw_ids, "step": step, "language": language}
        )
        if raw_id:
            payload["raw_id"] = raw_id

        if step < len(questions):
            messages.set_pending(
                self.db,
                chat_id=chat_id,
                kind="onboarding",
                payload=payload,
                expires_at=(utcnow() + timedelta(days=7)).isoformat(timespec="seconds"),
            )
            return self._remember(
                chat_id,
                TurnResult(text=questions[step], intent="onboarding"),
                language,
            )

        # Summary before finalising.  Nothing durable exists yet.
        messages.set_pending(
            self.db,
            chat_id=chat_id,
            kind="onboarding_confirm",
            payload=payload,
            expires_at=(utcnow() + timedelta(days=7)).isoformat(timespec="seconds"),
        )
        return self._remember(
            chat_id,
            TurnResult(
                text=self._onboarding_summary(answers, language),
                intent="onboarding",
            ),
            language,
        )

    def _complete_onboarding(
        self,
        chat_id: str,
        answers: list[str],
        language: str,
        *,
        raw_id: str | None = None,
        raw_ids: list[str] | None = None,
    ) -> str:
        ru = self._ru(language)
        ids = list(raw_ids or [])
        if raw_id and raw_id not in ids:
            ids.append(raw_id)
        summary = "; ".join(one_line(a)[:160] for a in answers if a.strip())
        content = NoteContent(
            title="Профиль пользователя" if ru else "User profile",
            type="fact",
            status="active",
            language=language,
            summary=(summary[:900] or ("Профиль создан при первичной настройке." if ru else "Profile created during onboarding.")),
            context="\n".join(f"- {one_line(a)}" for a in answers if a.strip()),
            tags=["profile", "onboarding"],
        )
        existing = notes_store.find(self.db, "user-profile")
        if existing is not None:
            notes_store.update_content(
                self.db, self.cfg, existing.id, content, raw_ids=ids,
                reason="Onboarding answers updated.", actor="alan",
            )
        else:
            notes_store.create(
                self.db, self.cfg, content, raw_ids=ids,
                reason="Created from onboarding answers.", actor="alan",
                slug="user-profile",
            )

        open_questions = [
            ("Какие знания уже есть в других инструментах и где они лежат?" if ru
             else "What knowledge already exists in other tools, and where?"),
            ("Какие темы считать чувствительными и исключить из проактивных сообщений?" if ru
             else "Which topics are sensitive and should be excluded from proactive messages?"),
            ("Какой уровень детализации предпочитаете по умолчанию?" if ru
             else "What default level of detail do you prefer?"),
        ]
        q_content = NoteContent(
            title="Вопросы после настройки" if ru else "Onboarding open questions",
            type="question",
            status="active",
            language=language,
            summary=("Что ещё нужно уточнить для полноценной работы." if ru
                     else "What still needs clarification for Alan to be useful."),
            open_questions=open_questions,
            tags=["onboarding", "questions"],
        )
        if notes_store.find(self.db, "onboarding-open-questions") is None:
            notes_store.create(
                self.db, self.cfg, q_content, raw_ids=[],
                reason="Created during onboarding.", actor="alan",
                slug="onboarding-open-questions",
            )

        prefs.set_value(self.db, "onboarding_done", "1", source="onboarding", actor="alan")
        return self._onboarding_text(
            language,
            "Completed",
            sir=self._sir(language),
            directions=summary[:400],
        )

    # ------------------------------------------------------------------
    # Import jobs
    # ------------------------------------------------------------------

    def _read_import(self, path: str | Path) -> tuple[list, str]:
        from ..ingest import telegram as tg
        from ..ingest import markdown as md

        source_path = Path(path)
        if source_path.is_dir():
            return md.read_folder(source_path), "markdown-import"
        if source_path.suffix.lower() == ".json":
            return tg.read_export(source_path), "telegram-export"
        if source_path.suffix.lower() in {".md", ".txt"}:
            return md.read_file(source_path), "file-import"
        raise AlanError(f"Unsupported import format: {source_path.suffix}")

    def start_import(
        self,
        path: str | Path,
        *,
        chat_id: str | int,
        make_notes: bool = True,
        progress: ProgressFn | None = None,
    ) -> dict:
        """Stage 1: parse, deduplicate, and persist raw captures immediately."""

        items, source = self._read_import(path)
        if not items:
            raise AlanError(
                "No importable messages found. Check that the file is a Telegram export or a notes folder."
            )

        job = imports_store.create(
            self.db,
            chat_id=chat_id,
            source=source,
            origin=str(Path(path)),
            options={"make_notes": make_notes},
        )
        self._import_items[job["id"]] = list(items)
        imports_store.add_items(self.db, job["id"], items)
        self._capture_pending(job)

        imports_store.set_status(self.db, job["id"], "running" if make_notes else "paused")
        return imports_store.get(self.db, job["id"]) or job

    def _capture_pending(self, job: dict) -> None:
        """Give every not-yet-captured item its immutable raw capture.

        This is called by both stages: a resumed job must never analyse an item
        whose original text is not already on disk.  It is idempotent, so a
        normal resume does nothing here.
        """

        job_id = job["id"]
        source = job["source"]
        chat_id = job["chat_id"]
        while True:
            batch = imports_store.next_pending(self.db, job_id, limit=500)
            if not batch:
                break
            for item in batch:
                digest = hashlib.sha256(item["text"].encode("utf-8")).hexdigest()
                existing = self.db.query_one(
                    "SELECT id FROM raw_captures WHERE content_hash = ? LIMIT 1", (digest,)
                )
                if existing is not None:
                    imports_store.mark(self.db, item["id"], "duplicate")
                    continue
                capture = raw.create(
                    self.db,
                    self.cfg,
                    chat_id=chat_id,
                    text=item["text"],
                    source=source,
                    language=item["language"]
                    or detect_language(item["text"], self.cfg.default_language),
                    received_at=item["received_at"],
                    imported=True,
                    media=item["media"] or None,
                )
                imports_store.mark(self.db, item["id"], "captured", raw_id=capture.id)
            imports_store.refresh_counts(self.db, job_id)

    async def process_import(
        self,
        job_id: str,
        *,
        limit: int | None = None,
        progress: ProgressFn | None = None,
        chat_id: str | int | None = None,
    ) -> ImportResult:
        """Stage 2: analyse captured items in priority order, resumably."""

        job = imports_store.get(self.db, job_id)
        if job is None:
            raise AlanError(f"No such import job: {job_id}")

        chat = str(chat_id if chat_id is not None else job["chat_id"])
        # Resume can follow a crash in stage 1; capture anything outstanding
        # before the first model call.
        self._capture_pending(job)
        imports_store.set_status(self.db, job_id, "running")
        analysed = 0

        while True:
            current = imports_store.get(self.db, job_id)
            if current is None or current["status"] in {"paused", "cancelled", "failed"}:
                break
            batch = imports_store.next_batch(self.db, job_id, limit=self.cfg.import_batch_size)
            if not batch:
                imports_store.set_status(self.db, job_id, "done")
                break

            for item in batch:
                current = imports_store.get(self.db, job_id)
                if current is None or current["status"] in {"paused", "cancelled"}:
                    break
                if limit is not None and analysed >= limit:
                    imports_store.set_status(self.db, job_id, "paused")
                    break
                try:
                    note_id, conflict_count = await self._analyze_import_item(item, chat)
                    if note_id:
                        imports_store.mark(self.db, item["id"], "done", note_id=note_id)
                    else:
                        imports_store.mark(self.db, item["id"], "skipped")
                    if conflict_count:
                        db_conflicts = int(
                            self.db.scalar(
                                "SELECT conflicts FROM import_jobs WHERE id = ?", (job_id,)
                            ) or 0
                        )
                        self.db.execute(
                            "UPDATE import_jobs SET conflicts = ? WHERE id = ?",
                            (db_conflicts + conflict_count, job_id),
                        )
                    analysed += 1
                except (LLMError, AlanError) as exc:
                    log.warning("import item failed: %s", exc)
                    imports_store.mark(self.db, item["id"], "failed", error=str(exc))
                except Exception as exc:  # pragma: no cover - one bad item must not stop the job
                    log.exception("import item failed unexpectedly")
                    imports_store.mark(self.db, item["id"], "failed", error=str(exc))
                await asyncio.sleep(0)

            imports_store.refresh_counts(self.db, job_id)
            if progress:
                await progress(self._import_progress(job_id))

            current = imports_store.get(self.db, job_id)
            if current and current["status"] == "paused":
                break

        job = imports_store.get(self.db, job_id)
        if job is None:
            raise AlanError(f"No such import job: {job_id}")
        imports_store.refresh_counts(self.db, job_id)
        job = imports_store.get(self.db, job_id) or job
        return ImportResult(
            items=job["total"],
            captures=job["captured"],
            notes=job["notes_made"],
            duplicates=job["duplicates"],
            conflicts=job["conflicts"],
            job_id=job_id,
        )

    async def _analyze_import_item(self, item: dict, chat_id: str) -> tuple[str | None, int]:
        text = item["text"]
        language = item["language"] or detect_language(text, self.cfg.default_language)
        candidates = await self._retrieve(text)
        assessment = await self.reasoner.assess(
            text=text,
            language=language,
            now=utcnow().isoformat(timespec="minutes"),
            timezone=self.cfg.timezone,
            projects=projects_store.list_all(self.db, include_provisional=True),
            candidates=candidates,
            history=[],
            preferences=prefs.prompt_block(self.db),
            lessons=lessons.prompt_block(self.db),
        )
        if assessment.needs_clarification:
            return None, 0
        result = apply_mod.apply_assessment(
            self.db, self.cfg, assessment, raw_id=item.get("raw_id"), actor="import"
        )
        await self._embed_note(result.note)
        return (result.note.id if result.note else None), len(result.conflicts)

    def _import_progress(self, job_id: str, language: str = "en") -> str:
        job = imports_store.get(self.db, job_id)
        if job is None:
            return "Import job not found, sir."
        ru = self._ru(language)
        if ru:
            return (
                f"Импорт: {job['captured']} из {job['total']} записей, сэр.\n"
                f"Создано raw-захватов: {job['captured']}.\n"
                f"Заметок: {job['notes_made']}. Возможных дублей: {job['duplicates']}. "
                f"Конфликтов: {job['conflicts']}.\n"
                f"Статус: {job['status']}."
            )
        return (
            f"Import: {job['captured']} of {job['total']} items, sir.\n"
            f"Raw captures created: {job['captured']}.\n"
            f"Notes: {job['notes_made']}. Possible duplicates: {job['duplicates']}. "
            f"Conflicts: {job['conflicts']}.\n"
            f"Status: {job['status']}."
        )

    def import_report(self, job_id: str, language: str = "en") -> str:
        return self._import_progress(job_id, language)

    def import_jobs(self, chat_id: str | int | None = None, *, limit: int = 10) -> list[dict]:
        return imports_store.list_jobs(self.db, chat_id=chat_id, limit=limit)

    def pause_import(self, job_id: str) -> bool:
        return imports_store.pause(self.db, job_id)

    def cancel_import(self, job_id: str) -> bool:
        return imports_store.cancel(self.db, job_id)

    async def resume_import(
        self, job_id: str, *, progress: ProgressFn | None = None, chat_id: str | int | None = None
    ) -> ImportResult:
        if not imports_store.resume(self.db, job_id):
            job = imports_store.get(self.db, job_id)
            if job is None:
                raise AlanError(f"No such import job: {job_id}")
        return await self.process_import(job_id, progress=progress, chat_id=chat_id)

    async def import_path(
        self,
        path: str | Path,
        *,
        chat_id: str | int,
        limit: int = 60,
        make_notes: bool = True,
        progress: ProgressFn | None = None,
    ) -> ImportResult:
        """Compatibility wrapper: start a job, analyse it, and report inventory."""

        if progress:
            await progress("Reading source, sir. Original text is stored before any analysis.")
        job = self.start_import(path, chat_id=chat_id, make_notes=make_notes, progress=progress)
        if progress:
            await progress(self._import_progress(job["id"]))
        result = await self.process_import(
            job["id"], limit=limit if make_notes else 0, progress=progress, chat_id=chat_id
        )

        inventory_md = ""
        if make_notes:
            inventory_md = await self._inventory_from_job(job["id"], chat_id)
            from .inventory import save_run

            save_run(
                self.db,
                chat_id=chat_id,
                source=job["source"],
                item_count=result.items,
                summary_md=inventory_md,
            )
        result.inventory = inventory_md
        return result

    async def _inventory_from_job(self, job_id: str, chat_id: str | int) -> str:
        items = self._import_items.get(job_id)
        if items is None:
            rows = imports_store.items(self.db, job_id, limit=5000)
            items = [
                SimpleNamespace(text=row["text"], received_at=row["received_at"], media=row["media"])
                for row in rows
            ]
        return await self._inventory(items, chat_id)

    async def _inventory(self, items: Sequence, chat_id: str | int) -> str:
        from .inventory import build_inventory_block

        if not items:
            return "Nothing was imported, sir. All existing notes are untouched."

        sample = sorted(items, key=lambda i: len(i.text), reverse=True)[:120]
        stats = (
            f"items: {len(items)}\n"
            f"notes in base: {notes_store.stats(self.db)['notes']}\n"
            f"raw captures: {raw.count(self.db)}"
        )
        try:
            draft = await self.reasoner.inventory(
                items=build_inventory_block(sample), stats=stats
            )
        except LLMError as exc:
            log.warning("inventory failed: %s", exc)
            return "Import complete, sir. The inventory analysis failed, but every item is preserved."

        from .inventory import render_inventory

        return render_inventory(draft)
