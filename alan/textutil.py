"""Small, dependency-free text helpers used across Alan.

Nothing here calls a model.  Language detection, slugs, dates and hashing must
behave identically no matter which reasoning provider is configured.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Iterable

# --- time -----------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def days_since(value: str) -> float:
    return (utcnow() - parse_iso(value)).total_seconds() / 86400.0


def hours_since(value: str) -> float:
    return (utcnow() - parse_iso(value)).total_seconds() / 3600.0


RU_MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
EN_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def human_datetime(value: datetime | str, lang: str = "en") -> str:
    """'11 November, 14:24' in English, '11 ноября, 14:24' in Russian."""

    dt = parse_iso(value) if isinstance(value, str) else value
    month = (RU_MONTHS if lang.startswith("ru") else EN_MONTHS)[dt.month - 1]
    if lang.startswith("ru"):
        return f"{dt.day} {month}, {dt:%H:%M}"
    return f"{dt.day} {month}, {dt:%H:%M}"


# --- language -------------------------------------------------------------

_CYRILLIC = re.compile(r"[\u0400-\u04FF]")
_LATIN = re.compile(r"[A-Za-z]")
_UA_MARKERS = set("іїєґІЇЄҐ")


def detect_language(text: str, default: str = "en") -> str:
    """Cheap, deterministic language guess good enough for routing.

    Returns a BCP-47-ish primary tag: ru, uk, en, or ``default``.
    """

    sample = text[:4000]
    cyrillic = len(_CYRILLIC.findall(sample))
    latin = len(_LATIN.findall(sample))
    if cyrillic == 0 and latin == 0:
        return default

    # A purely Cyrillic sample is Cyrillic even when it is one short word
    # ("Да."): dropping it to the default would switch the conversation's
    # language on a one-word answer.
    if cyrillic and (latin == 0 or cyrillic >= max(3, latin * 0.4)):
        if sum(1 for ch in sample if ch in _UA_MARKERS) >= 2:
            return "uk"
        return "ru"
    return "en"


def language_name(code: str) -> str:
    return {
        "ru": "Russian",
        "uk": "Ukrainian",
        "en": "English",
        "de": "German",
        "fr": "French",
        "es": "Spanish",
        "it": "Italian",
        "pt": "Portuguese",
        "pl": "Polish",
        "tr": "Turkish",
        "kk": "Kazakh",
    }.get(code.split("-")[0].lower(), code)


# --- slugs ----------------------------------------------------------------

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g",
}


def transliterate(text: str) -> str:
    out: list[str] = []
    for ch in text:
        lower = ch.lower()
        if lower in _TRANSLIT:
            repl = _TRANSLIT[lower]
            out.append(repl.capitalize() if ch.isupper() and repl else repl)
        else:
            out.append(ch)
    return "".join(out)


def slugify(text: str, *, fallback: str = "note", max_len: int = 64) -> str:
    base = transliterate(unicodedata.normalize("NFKC", text))
    base = base.lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    base = re.sub(r"-{2,}", "-", base)
    if not base:
        base = fallback
    if len(base) > max_len:
        base = base[:max_len].rstrip("-")
    return base or fallback


# --- misc -----------------------------------------------------------------

def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_ws(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def clamp(text: str, limit: int, suffix: str = "…") -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def bullet(items: Iterable[str]) -> str:
    return "\n".join(f"- {item}" for item in items if str(item).strip())


def one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def title_from_text(text: str, limit: int = 70) -> str:
    """Derive a readable title when the model did not supply a good one."""

    first = one_line(text)
    for sep in (". ", "? ", "! ", "\n"):
        if sep in first[: limit + 20]:
            first = first.split(sep, 1)[0]
            break
    first = first.strip(" .!?…,;:-—")
    return clamp(first, limit) or "Untitled"


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many
