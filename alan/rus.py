"""Russian-aware text normalization for indexing and querying.

`unicode61` tokenizes Cyrillic but does not reduce inflection, so
"архитектуре", "архитектуры" and "архитектурный" become three unrelated
tokens.  Alan compensates in Python, before FTS5 sees the text:

* lowercase, ё -> е, punctuation removed;
* a compact Snowball-style Russian stemmer;
* a four-letter root token for every word of six characters or more, which
  bridges stems the suffix rules cannot join (решение / решил -> реш).

Normalization is only ever applied to the index and to queries.  Raw captures
and Markdown notes keep the user's exact words.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[0-9A-Za-z_\u0400-\u04FF]+", re.UNICODE)

_VOWELS = set("аеиоуыэюя")
_CYRILLIC = re.compile(r"[\u0400-\u04FF]")

# Order matters: longest suffix first inside every group.
_PERFECTIVE_GERUND = ("вшись", "вши", "в")
_ADJECTIVE = (
    "ими", "ыми", "его", "ого", "ему", "ому", "ее", "ие", "ые", "ое",
    "ей", "ий", "ый", "ой", "ем", "им", "ым", "ом", "их", "ых", "ую",
    "юю", "ая", "яя", "ою", "ею",
)
_PARTICIPLE = ("ем", "нн", "вш", "ющ", "щ")
_VERB = (
    "ила", "ыла", "ена", "ейте", "уйте", "ите", "или", "ыли", "ило",
    "ыло", "ено", "ует", "уют", "ены", "ить", "ыть", "ишь", "ешь",
    "ете", "йте", "ят", "ит", "ыт", "ую", "ла", "на", "ли", "ло",
    "но", "ет", "ют", "ны", "ть", "ей", "уй", "ил", "ыл", "им",
    "ым", "ен", "ем", "н", "л", "й", "ю",
)
_NOUN = (
    "иями", "ями", "ами", "иях", "иям", "ием", "ах", "ях", "ов", "ев",
    "ие", "ье", "еи", "ии", "ией", "ей", "ой", "ий", "ям", "ем", "ам",
    "ом", "ию", "ью", "ия", "ья", "а", "е", "и", "й", "о", "у", "ы",
    "ь", "я", "ю",
)

STOPWORDS = frozenset(
    """
    и в во не что он на я с со как а то все она так его но да ты к у же вы
    за бы по только ее мне было вот от меня еще нет о из ему теперь когда
    даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж
    вам ведь там потом себя ничего ей может они тут где есть надо ней для
    мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж
    тогда кто этот того потому этого какой совсем ним здесь этом один почти
    мой тем чтобы нее сейчас были куда зачем всех никогда можно при наконец
    два об другой хоть после над больше тот через эти нас про всего них
    какая много разве три эту моя впрочем хорошо свою этой перед иногда
    лучше чуть том нельзя такой им более всегда конечно всю между
    the a an and or of to in is are was were do does did what how why when
    my me i you it that this for on at with about have has had will would
    can could should be been being from as by but not no so if then than
    there their them we our your his her its
    """.split()
)


def normalize(text: str) -> str:
    """Lowercase, unify ё, collapse punctuation into single spaces."""

    lowered = (text or "").lower().replace("ё", "е")
    tokens = _TOKEN_RE.findall(lowered)
    return " ".join(tokens)


def _rv_index(word: str) -> int:
    for index, char in enumerate(word):
        if char in _VOWELS:
            return index + 1
    return -1


def _strip(rv: str, suffixes: tuple[str, ...]) -> str:
    for suffix in suffixes:
        if rv.endswith(suffix) and len(rv) - len(suffix) >= 2:
            return rv[: -len(suffix)]
    return rv


def stem(word: str) -> str:
    """Compact Russian stemmer.  Latin words are returned lowercased."""

    lowered = (word or "").lower().replace("ё", "е")
    if not _CYRILLIC.search(lowered):
        return lowered
    rv_start = _rv_index(lowered)
    if rv_start <= 0:
        return lowered
    prefix, rv = lowered[:rv_start], lowered[rv_start:]

    rv = _strip(rv, _PERFECTIVE_GERUND)
    rv = _strip(rv, _PARTICIPLE)
    stripped = _strip(rv, _ADJECTIVE)
    if stripped == rv:
        stripped = _strip(rv, _VERB)
    if stripped == rv:
        stripped = _strip(rv, _NOUN)
    rv = stripped

    if rv.endswith("и") and len(rv) > 2:
        rv = rv[:-1]
    for suffix in ("ость", "ост"):
        if rv.endswith(suffix) and len(rv) - len(suffix) >= 3:
            rv = rv[: -len(suffix)]
            break
    if rv.endswith("нн"):
        rv = rv[:-1]
    elif rv.endswith("ь") and len(rv) > 2:
        rv = rv[:-1]
    return prefix + rv


def _roots(token: str) -> tuple[str, ...]:
    """Two prefix keys that survive suffix differences between related words.

    решение and решил stem to решен and реши; the shared ``реш`` prefix is what
    lets them find each other.  Prefixes are only produced for longer words so
    short function words cannot cause false matches.
    """

    if len(token) < 5 or token.isdigit():
        return ()
    return (token[:3], token[:4])


def index_text(text: str) -> tuple[str, str]:
    """Return ``(stems, roots)`` for the FTS index."""

    stems: list[str] = []
    roots: list[str] = []
    for token in _TOKEN_RE.findall((text or "").lower().replace("ё", "е")):
        stems.append(stem(token))
        roots.extend(_roots(token))
    return " ".join(stems), " ".join(dict.fromkeys(roots))


def query_terms(text: str, *, limit: int = 24) -> list[str]:
    """Stems plus root tokens for a search query, common words dropped."""

    stems: list[str] = []
    roots: list[str] = []
    for token in _TOKEN_RE.findall((text or "").lower().replace("ё", "е")):
        if token in STOPWORDS or len(token) < 2:
            continue
        stems.append(stem(token))
        roots.extend(_roots(token))
    ordered = list(dict.fromkeys(stems + roots))
    if not ordered:
        # A query made entirely of stopwords is still a query.
        ordered = [stem(token) for token in _TOKEN_RE.findall((text or "").lower())][:limit]
    return ordered[:limit]
