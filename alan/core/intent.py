"""Deterministic intent classification, before any model call.

The product rule is intent before action.  A control command, a complaint, a
refusal, or a meta-question must never be stored as knowledge or consumed as
an onboarding answer.  This module recognises those cases without a model, in a
fixed priority order:

1. safety or destructive action
2. flow control
3. explicit correction or complaint
4. meta-questions about Alan (policy, model, memory)
5. confirmation answers (yes/no for a pending summary or deletion)
6. refusals and note controls
7. explicit preference
8. clarification answer
9. question or search
10. onboarding answer
11. small talk
12. unknown / capture

Flow control and corrections always win over onboarding and capture.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(slots=True)
class Intent:
    kind: str
    action: str = ""
    secondary: tuple[str, ...] = field(default_factory=tuple)
    confidence: float = 0.8

    @property
    def is_flow(self) -> bool:
        return self.kind == "flow_control"

    @property
    def is_non_answer(self) -> bool:
        return self.kind in {
            "safety", "flow_control", "correction", "complaint",
            "show_policy", "show_model", "memory", "no_record",
            "delete_last", "make_note",
        }


def _norm(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"[«»\"“”„`]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n.!?,;:—-")


def _has(patterns: tuple[str, ...], text: str) -> bool:
    return any(pattern in text for pattern in patterns)


_SAFETY = (
    "удали все заметки", "удали всё", "удали все данные", "удали базу",
    "стереть всё", "стереть все", "очисти базу", "drop database",
    "delete everything", "delete all notes", "wipe all", "erase everything",
    "format the database",
)

_FLOW_STOP = (
    "останови настройку", "останови опрос", "остановить настройку",
    "отмени настройку", "отмена настройки", "отмени опрос", "прекрати настройку",
    "хватит настройки", "не надо настройку", "не нужно настройку",
    "stop setup", "stop onboarding", "stop the setup", "stop the onboarding",
    "cancel setup", "cancel the setup", "cancel onboarding", "cancel the onboarding",
    "pause setup", "pause onboarding", "pause the setup",
    "останови", "стоп", "пауза", "позже", "не сейчас", "не сейчас.",
    "хватит", "пропусти", "пропустить", "прекрати",
    "stop", "pause", "not now", "later", "skip this", "skip", "hold on",
)
_FLOW_RESUME = (
    "продолжи настройку", "продолжим настройку", "продолжи опрос",
    "продолжим опрос", "вернись к настройке", "вернемся к настройке",
    "вернёмся к настройке", "давай продолжим настройку",
    "continue setup", "continue the setup", "continue onboarding",
    "continue the onboarding", "resume setup", "resume onboarding",
    "resume the setup", "resume the onboarding",
)
_SETUP_WORDS = ("настройк", "опрос", "setup", "onboarding", "анкет")

_POLICY = (
    "системный промпт", "системный промт", "системный prompt", "твой промпт",
    "твой промт", "покажи промпт", "покажи свой промпт", "покажи правила",
    "твои правила", "твоих правил", "ваши правила", "твои инструкции",
    "твоих инструкций", "как ты устроен", "как ты работаешь",
    "какие у тебя правила", "правила поведения",
    "system prompt", "show your prompt", "show me your prompt", "your prompt",
    "your rules", "your instructions", "show your rules", "how do you work",
    "how are you configured", "what are your rules",
)
_MODEL = (
    "какая модель", "какой моделью", "какие модели", "какую модель",
    "модель используешь", "какая у тебя модель", "назови модель",
    "what model", "which model", "what models", "model are you using",
    "what llm", "which llm",
)
_MEMORY = (
    "что ты помнишь", "что помнишь", "что я только что сказал",
    "что я только что написал", "что я сейчас сказал",
    "какое моё прошлое сообщение", "какое мое прошлое сообщение",
    "какое моё предыдущее сообщение", "какое мое предыдущее сообщение",
    "что было в прошлом сообщении", "объясни свой предыдущий ответ",
    "объясни свое предыдущий ответ", "объясни предыдущий ответ",
    "я имел в виду предыдущее", "я имел в виду прошлое",
    "what did i just say", "what did i say", "what was my previous question",
    "what was my last question", "what was my previous message",
    "what was my last message", "what do you remember",
    "explain your previous answer", "explain your last answer",
    "i meant the previous", "i meant the last message",
)

_COMPLAINT = (
    "зачем ты это записал", "зачем ты записала", "зачем ты это сохранил",
    "почему ты это записал", "почему ты это сохранил", "я тебе не отвечал",
    "я тебе не отвечала", "я не отвечал", "я не это говорил", "я этого не говорил",
    "я не это имел в виду", "я не то имел в виду", "ты меня не понял",
    "ты не понял", "ты меня не поняла", "ты ошибся", "ты ошибаешься",
    "это не то, что я", "я не просил записывать", "я не просила записывать",
    "why did you record", "why did you save", "i did not say", "i didn't say",
    "i never said", "you misunderstood", "you got me wrong", "that is not what i",
    "i did not ask you to save", "i didn't ask you to save",
)

_NO_RECORD = (
    "не записывай это", "не записывай", "ничего не записывай", "не надо записывать",
    "не нужно записывать", "не сохраняй это", "не сохраняй", "без заметки",
    "не создавай заметку", "просто ответь", "просто скажи", "не записывай как урок",
    "do not record this", "don't record this", "do not save this", "don't save this",
    "no note", "without a note", "just answer", "just tell me",
)
_DELETE_LAST = (
    "удали последнюю заметку", "удалить последнюю заметку", "удали последнюю запись",
    "удалить последнюю запись", "удали последний note", "delete the last note",
    "delete last note", "remove the last note", "remove last note",
)
_MAKE_NOTE = (
    "сделай заметку", "создай заметку", "запиши это", "запиши как заметку",
    "сохрани это как заметку", "make a note", "create a note", "save this as a note",
    "make this a note",
)

_PREFERENCE = (
    "всегда ", "никогда ", "с этого момента", "впредь ", "я предпочитаю",
    "мне нравится, когда", "мне не нравится, когда", "пожалуйста, всегда",
    "always ", "never ", "from now on", "i prefer", "i would rather",
    "please always", "please never",
)

_QUESTION_START = (
    "что", "кто", "где", "когда", "почему", "зачем", "как", "какой", "какая",
    "какие", "каких", "сколько", "чем", "куда", "можно ли", "есть ли",
    "what", "who", "where", "when", "why", "how", "which", "how many",
    "is there", "are there", "can you", "could you", "do i", "did i",
)
_SEARCH = (
    "найди", "поищи", "покажи заметки", "покажи записи", "поиск",
    "search", "find", "look up",
)
_SMALLTALK = (
    "привет", "здравствуйте", "добрый день", "добрый вечер", "доброе утро",
    "спасибо", "благодарю", "понял", "поняла", "ок", "окей", "хорошо",
    "hello", "hi", "hey", "thanks", "thank you", "ok", "okay", "got it",
)

_YES = re.compile(
    r"^\s*(да|ага|угу|верно|конечно|подтверждаю|сохрани|сохраняй|yes|yeah|yep|sure|ok|okay|confirm)\b",
    re.IGNORECASE,
)
_NO = re.compile(
    r"^\s*(нет|не надо|не нужно|не сохраняй|не сохраняй|отмена|no|nope|don't|do not|cancel)\b",
    re.IGNORECASE,
)


def _is_short(text: str, limit: int = 8) -> bool:
    return len(text.split()) <= limit


def _setup_explicit(text: str) -> bool:
    return _has(_SETUP_WORDS, text)


def _flow_stop(text: str, pending_kind: str | None) -> bool:
    if not _has(_FLOW_STOP, text):
        return False
    # A long message that merely contains "later" is content, not control.
    if _is_short(text):
        return True
    return _setup_explicit(text)


def classify(text: str, *, pending_kind: str | None = None) -> Intent:
    normalized = _norm(text)
    words = normalized.split()

    # 1. Safety and destructive requests.
    if _has(_SAFETY, normalized):
        return Intent("safety", action="refuse_destructive", confidence=0.99)

    # 2. Flow control.  Always beats onboarding, clarification, and capture.
    if _has(_FLOW_RESUME, normalized):
        return Intent("flow_control", action="resume", confidence=0.98)
    if _flow_stop(normalized, pending_kind):
        secondary: list[str] = []
        if _has(_POLICY, normalized):
            secondary.append("show_policy")
        if _has(_MODEL, normalized):
            secondary.append("show_model")
        return Intent(
            "flow_control",
            action="pause",
            secondary=tuple(secondary),
            confidence=0.98,
        )

    # 3. Explicit correction or complaint about behaviour.
    if _has(_COMPLAINT, normalized):
        return Intent("complaint", action="recover", confidence=0.95)

    # 4. Meta-questions about Alan itself, answered without capture.
    wants_policy = _has(_POLICY, normalized)
    wants_model = _has(_MODEL, normalized)
    if wants_policy or wants_model:
        secondary = []
        if wants_policy:
            secondary.append("show_policy")
        if wants_model:
            secondary.append("show_model")
        return Intent(
            "show_policy" if wants_policy else "show_model",
            action="disclose",
            secondary=tuple(secondary),
            confidence=0.95,
        )

    # 5. Confirmation states consume a plain yes/no before note controls.  A
    # control command was already handled above, so "нет, покажи промпт" still
    # discloses, while "нет, не сохраняй профиль" is a refusal of the summary.
    if pending_kind in {"onboarding_confirm", "lesson_confirm", "delete_confirm"}:
        if _YES.match(text or ""):
            return Intent("affirm", action="confirm", confidence=0.97)
        if _NO.match(text or ""):
            return Intent("deny", action="reject", confidence=0.97)

    # 6. Explicit no-record / answer-only, and note controls.
    if _has(_NO_RECORD, normalized):
        return Intent("no_record", action="answer_only", confidence=0.92)
    if _has(_DELETE_LAST, normalized):
        return Intent("delete_last", action="confirm_delete", confidence=0.95)
    if _has(_MAKE_NOTE, normalized):
        return Intent("make_note", action="capture_previous", confidence=0.9)

    # 7. Memory questions about the current conversation.
    if _has(_MEMORY, normalized):
        return Intent("memory", action="recall", confidence=0.95)

    # 8. Explicit preference.  The model still decides whether it is durable.
    if _has(_PREFERENCE, normalized):
        return Intent("preference", action="propose_rule", confidence=0.85)

    # 9. Clarification answer.
    if pending_kind == "clarify" and not _looks_like_question(normalized):
        return Intent("clarification_answer", action="resolve_clarification", confidence=0.85)

    # 10. Search and questions.
    if _has(_SEARCH, normalized) and _is_short(normalized, limit=12):
        return Intent("search", action="retrieve", confidence=0.85)
    if _looks_like_question(normalized):
        return Intent("question", action="answer", confidence=0.8)

    # 11. Onboarding answers: only messages that are not control or meta.
    if pending_kind == "onboarding":
        if _is_short(normalized, limit=2) and _has(_SMALLTALK, normalized):
            return Intent("smalltalk", action="reply", confidence=0.8)
        return Intent("answer", action="onboarding_answer", confidence=0.8)

    # 12. Small talk.
    if _has(_SMALLTALK, normalized) and _is_short(normalized, limit=5):
        return Intent("smalltalk", action="reply", confidence=0.8)

    # 13. Capture request or ordinary statement.
    if len(words) <= 2:
        return Intent("unknown", action="ask", confidence=0.5)
    return Intent("capture", action="apply", confidence=0.8)


def _looks_like_question(text: str) -> bool:
    if text.endswith("?"):
        return True
    first = text.split()[0] if text.split() else ""
    return any(text.startswith(word + " ") or text == word for word in _QUESTION_START)
