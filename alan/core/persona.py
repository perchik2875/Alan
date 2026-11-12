"""Voice: address, policy disclosure, confirmation templates, and sources.

Confirmation text is generated deterministically from applied data rather than
by the model, so the same action reads the same way regardless of provider.
The persona is deliberately capable of explaining itself: nothing here is a
secret from the user, only credentials and private paths are withheld.
"""

from __future__ import annotations

import re
from typing import Sequence

from ..models import Note, Project
from ..textutil import human_datetime

TYPE_LABELS = {
    "en": {
        "idea": "Idea", "decision": "Decision", "task": "Task", "agreement": "Agreement",
        "fact": "Fact", "question": "Question", "plan": "Plan",
        "observation": "Observation", "reference": "Reference",
    },
    "ru": {
        "idea": "Идея", "decision": "Решение", "task": "Задача", "agreement": "Договорённость",
        "fact": "Факт", "question": "Вопрос", "plan": "План",
        "observation": "Наблюдение", "reference": "Ссылка",
    },
}

STRINGS = {
    "en": {
        "sir": "sir",
        "recorded": "Recorded, {sir}.",
        "updated": "Updated, {sir}.",
        "clarify_one": "Understood, {sir}. One clarification:",
        "clarify_many": "Understood, {sir}. {n} clarifications:",
        "linked_to": "Linked to: {items}.",
        "source": "Source: raw capture from {when}.",
        "open_questions": "Open questions: {n}.",
        "sources": "Sources:",
        "proposed_project": "Proposed project: {name}. Confirm it and I will keep it permanently.",
        "no_answer": "I have nothing in the knowledge base on that, {sir}.",
        "nothing_found": "There are no matching records in the knowledge base, {sir}.",
        "error": "Something failed on my side, {sir}. The message is preserved as a raw capture.",
        "lesson_ask": (
            "Is that a one-time instruction, or a lasting preference I should apply from now on, {sir}?"
        ),
        "lesson_created": "Recorded as a lesson, {sir}: {text}\nDeactivate it with /forget {id}.",
        "lesson_declined": "Understood, {sir}. I will apply it this time and not treat it as a rule.",
        "preference_set": "Preference saved, {sir}: {key} = {value}.",
        "external_edit": (
            "I detected an external edit to the note “{title}”, {sir}. "
            "I created version {version} and re-indexed it."
        ),
        "external_edit_question": "Should I also update its links, tags, and status metadata?",
        "metadata_refreshed": "Metadata refreshed, {sir}. Your text is unchanged.",
        "metadata_kept": "Left as-is, {sir}. Your text and metadata stay untouched.",
        "nothing_remembered": "I have no earlier turns in this conversation, {sir}.",
        "policy_header": "These are the rules I actually run on, {sir}. Nothing here is hidden from you.",
    },
    "ru": {
        "sir": "сэр",
        "recorded": "Записано, {sir}.",
        "updated": "Обновлено, {sir}.",
        "clarify_one": "Понял, {sir}. Одно уточнение:",
        "clarify_many": "Понял, {sir}. Уточнений: {n}.",
        "linked_to": "Связано с: {items}.",
        "source": "Источник: исходное сообщение от {when}.",
        "open_questions": "Открытых вопросов: {n}.",
        "sources": "Источники:",
        "proposed_project": "Предлагаемый проект: {name}. Подтвердите — и я закреплю его.",
        "no_answer": "В базе знаний об этом ничего нет, {sir}.",
        "nothing_found": "В базе знаний нет подходящих записей, {sir}.",
        "error": "На моей стороне произошёл сбой, {sir}. Сообщение сохранено как исходное.",
        "lesson_ask": (
            "Это разовая инструкция или устойчивое предпочтение, которое мне следует "
            "учитывать в дальнейшем, {sir}?"
        ),
        "lesson_created": "Записал как урок, {sir}: {text}\nОтключить: /forget {id}.",
        "lesson_declined": "Понял, {sir}. Применю это разово и не буду записывать как правило.",
        "preference_set": "Предпочтение сохранено, {sir}: {key} = {value}.",
        "external_edit": (
            "Обнаружил внешнее изменение заметки «{title}», {sir}. "
            "Создал версию {version} и обновил поиск."
        ),
        "external_edit_question": "Хотите, чтобы я также обновил связанные записи, теги и статус?",
        "metadata_refreshed": "Метаданные обновлены, {sir}. Ваш текст не изменён.",
        "metadata_kept": "Оставил как есть, {sir}. Ваш текст и метаданные не тронуты.",
        "nothing_remembered": "В этом разговоре пока нет предыдущих сообщений, {sir}.",
        "policy_header": "Вот правила, по которым я работаю, {sir}. Ничего из этого от вас не скрыто.",
    },
}

# Never echoed back, even in an honest explanation of configuration.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"AQ\.[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"),
    re.compile(r"(?i)(api[_ -]?key|token|secret|password)\s*[:=]\s*\S+"),
)


def strings(lang: str) -> dict[str, str]:
    return STRINGS["ru"] if (lang or "").lower().startswith(("ru", "uk")) else STRINGS["en"]


def address(lang: str) -> str:
    return strings(lang)["sir"].capitalize()


def ensure_address(text: str, lang: str) -> str:
    """Address the user once, inside the first sentence rather than as a prefix.

    "Записано." becomes "Записано, сэр." — not "Сэр. Записано." The latter is
    the templated, unnatural register the product explicitly rejects.
    """

    text = (text or "").strip()
    if not text:
        return text
    lowered = text.lower()
    if "sir" in lowered or "сэр" in lowered:
        return text
    sir = strings(lang)["sir"]
    match = re.search(r"[.!?](?=\s|$)", text)
    if match and match.start() > 2:
        before = text[: match.start()].rstrip()
        return f"{before}, {sir}{text[match.start():]}"
    return f"{text.rstrip('.')}, {sir}."


def redact(text: str, *, data_dir: object | None = None) -> str:
    """Remove credentials and private absolute paths from an outgoing reply."""

    cleaned = text or ""
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    if data_dir:
        base = str(data_dir)
        if base:
            cleaned = cleaned.replace(base, "<data>")
            cleaned = cleaned.replace(base.replace("\\", "\\\\"), "<data>")
    return cleaned


_PLACEHOLDER = re.compile(r"\{(sir|user|model|provider|embedding|detail|memory|answer|summary|directions|resume_command|question|step|total|date|timezone)\}")


def finalize(text: str, *, data_dir: object | None = None) -> str:
    """Redact secrets and guarantee that no template placeholder leaks.

    The prompt loader refuses unresolved placeholders when composing prompts.
    This is the last line of defence for deterministic reply strings: a typo
    must never reach the user as ``{sir}``.
    """

    cleaned = redact(text, data_dir=data_dir)
    if _PLACEHOLDER.search(cleaned):
        cleaned = _PLACEHOLDER.sub("", cleaned)
        cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned


def policy_text(
    language: str,
    *,
    model: str = "",
    provider: str = "",
    embedding: str = "",
    detail: str = "",
    memory_note: str = "",
    prompts_path: str = "",
    local: str = "",
) -> str:
    """Explain the operating rules and configuration without secrets."""

    s = strings(language)
    if (language or "").lower().startswith(("ru", "uk")):
        lines = [
            s["policy_header"].format(sir=s["sir"]),
            "",
            "Поведение:",
            "- отвечаю на языке вашего последнего сообщения; исходные тексты сохраняются без изменений;",
            "- обращаюсь один раз на ответ, естественно, а не в каждом предложении;",
            "- не льщу и не соглашаюсь автоматически; отделяю факт, вывод, допущение и неизвестное;",
            "- сначала сохраняю исходное сообщение, затем строю заметку; исходник неизменяем;",
            "- Markdown-заметка — источник содержания, JSON — метаданные, поиск — производный индекс;",
            "- урок создаю только по явному устойчивому правилу; вопрос или жалоба уроком не становится;",
            "- если правило неоднозначно, спрашиваю, разовое оно или постоянное;",
            "- команды управления выполнены раньше сохранения: отказ, пауза и жалоба не становятся знанием;",
            "- память: рабочий контекст разговора, эпизоды, факты и предпочтения, процедурные уроки;",
            "- секреты не раскрываю: API-ключи, токены, пароли и приватные пути скрыты.",
        ]
        if model or provider:
            lines.append(f"- модель рассуждений: {provider}/{model}." if provider else f"- текущая модель: {model}.")
        if local:
            lines.append(f"- размещение модели: {local}.")
        if embedding:
            lines.append(f"- эмбеддинги: {embedding}.")
        if detail:
            lines.append(f"- желаемая детализация: {detail}.")
        if memory_note:
            lines.append(f"- память: {memory_note}")
        lines.append(
            "- от модели не зависят: хранилище и формат заметок, исходные захваты, память, "
            "приватность, правила интентов, обработка конфликтов и язык."
        )
        if prompts_path:
            lines.append(f"- редактируемые промпты: `{prompts_path}/`.")
        lines.append("")
        lines.append(
            "Правила поведения лежат в Markdown-файлах, а не в коде. "
            "Я могу пересказать любой раздел. Ключи, токены и приватные пути не показываю."
        )
        return "\n".join(lines)

    lines = [
        s["policy_header"].format(sir=s["sir"]),
        "",
        "Behaviour:",
        "- I reply in the language of your latest message; original text is stored unchanged;",
        "- I address you once per reply, naturally, not at the start of every sentence;",
        "- I do not flatter or agree automatically; I separate fact, inference, assumption, and unknown;",
        "- I store the raw capture first, then build a note; the raw capture is immutable;",
        "- the Markdown note is the content source, JSON is metadata, indexes are derived;",
        "- a lesson is created only from an explicit, durable preference or correction; a question or complaint never becomes one;",
        "- when intent is ambiguous I ask whether it is one-time or lasting;",
        "- control commands run before storage: a refusal, pause, or complaint never becomes knowledge;",
        "- memory has four layers: working conversation, episodic events, semantic facts/preferences, procedural lessons;",
        "- secrets stay hidden: API keys, tokens, passwords, and private paths are redacted.",
    ]
    if model or provider:
        lines.append(f"- reasoning model: {provider}/{model}." if provider else f"- current model: {model}.")
    if local:
        lines.append(f"- model location: {local}.")
    if embedding:
        lines.append(f"- embeddings: {embedding}.")
    if detail:
        lines.append(f"- requested detail level: {detail}.")
    if memory_note:
        lines.append(f"- memory: {memory_note}")
    lines.append(
        "- model-independent: vault and note format, raw captures, memory, privacy, "
        "intent rules, conflict handling, and language."
    )
    if prompts_path:
        lines.append(f"- editable prompts: `{prompts_path}/`.")
    lines.append("")
    lines.append(
        "The behavioural rules live in Markdown prompt files, not in the code. "
        "I can summarise any section. Keys, tokens, and private paths are not shown."
    )
    return "\n".join(lines)


def _type_label(note_type: str, lang: str) -> str:
    table = TYPE_LABELS["ru"] if (lang or "").lower().startswith(("ru", "uk")) else TYPE_LABELS["en"]
    return table.get(note_type, note_type.capitalize())


def confirmation(
    note: Note,
    *,
    created: bool,
    language: str,
    project: Project | None = None,
    project_is_new: bool = False,
    related_titles: Sequence[str] = (),
    raw_received_at: str | None = None,
    open_questions: int = 0,
) -> str:
    s = strings(language)
    head = s["recorded"] if created else s["updated"]
    lines = [head.format(sir=s["sir"])]

    label = _type_label(note.type, language)
    substance = (note.summary or note.content.get("decision") or "").strip()
    if substance:
        lines.append(f"{label}: {substance}")

    linked: list[str] = []
    if project is not None:
        linked.append(project.name)
    linked.extend(t for t in related_titles if t not in linked)
    if linked:
        lines.append(s["linked_to"].format(items="; ".join(linked[:4])))

    if open_questions > 0:
        lines.append(s["open_questions"].format(n=open_questions))

    if raw_received_at:
        lines.append(s["source"].format(when=human_datetime(raw_received_at, language)))

    if project_is_new and project is not None:
        lines.append("")
        lines.append(s["proposed_project"].format(name=project.name))

    return "\n".join(lines)


def clarification(questions: Sequence[str], language: str) -> str:
    s = strings(language)
    questions = [q for q in questions if q.strip()][:2]
    if not questions:
        return ""
    if len(questions) == 1:
        head = s["clarify_one"].format(sir=s["sir"])
        return f"{head}\n{questions[0]}"
    head = s["clarify_many"].format(sir=s["sir"], n=len(questions))
    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, start=1))
    return f"{head}\n{numbered}"


def lesson_created(language: str, *, lesson_id: str, text: str) -> str:
    s = strings(language)
    return s["lesson_created"].format(sir=s["sir"], id=lesson_id, text=text)


def lesson_question(language: str) -> str:
    s = strings(language)
    return s["lesson_ask"].format(sir=s["sir"])


def nothing_found(language: str) -> str:
    s = strings(language)
    return s["nothing_found"].format(sir=s["sir"])


def external_edit_notice(language: str, *, title: str, version: int) -> str:
    s = strings(language)
    return (
        s["external_edit"].format(sir=s["sir"], title=title, version=version)
        + "\n"
        + s["external_edit_question"]
    )


def sources_block(notes: Sequence[Note], language: str) -> str:
    if not notes:
        return ""
    s = strings(language)
    lines = [s["sources"]]
    seen: set[str] = set()
    for note in notes:
        if note.id in seen:
            continue
        seen.add(note.id)
        lines.append(f"- {note.title} · `{note.slug}`")
    return "\n".join(lines)
