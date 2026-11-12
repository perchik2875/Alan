"""Identifier generation.

IDs are short, URL-safe, and prefixed by record type so that a glance at a log
line is enough to know what is being referenced.
"""

from __future__ import annotations

import secrets

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lowercase


def _token(length: int = 10) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def raw_id() -> str:
    return "r_" + _token()


def note_id() -> str:
    return "n_" + _token()


def project_id() -> str:
    return "p_" + _token()


def link_id() -> str:
    return "l_" + _token()


def conflict_id() -> str:
    return "c_" + _token()


def lesson_id() -> str:
    return "ls_" + _token()


def reminder_id() -> str:
    return "rm_" + _token()


def inventory_id() -> str:
    return "inv_" + _token()


def import_id() -> str:
    return "imp_" + _token()


def dedupe_suffix(existing: set[str], base: str) -> str:
    """Return ``base`` or ``base-2``, ``base-3``… so it is not in ``existing``."""

    if base not in existing:
        return base
    counter = 2
    while f"{base}-{counter}" in existing:
        counter += 1
    return f"{base}-{counter}"
