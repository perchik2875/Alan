"""Shared types for importers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ImportItem:
    text: str
    received_at: str
    language: str | None = None
    author: str = ""
    media: list[dict[str, Any]] = field(default_factory=list)
    origin_id: str = ""
