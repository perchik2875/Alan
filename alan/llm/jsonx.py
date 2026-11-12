"""Lenient JSON extraction and validation.

Models are asked for strict JSON, but "strict" is a spectrum.  This module
recovers the JSON object from fenced blocks, prose preambles, and trailing
commas before validation, and raises a precise error when that is impossible.
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..errors import LLMOutputInvalid

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def _strip_fences(text: str) -> str:
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _repair(candidate: str) -> str:
    candidate = candidate.replace("\u201c", '"').replace("\u201d", '"')
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    return candidate


def extract_json(text: str) -> dict[str, Any]:
    if not text or not text.strip():
        raise LLMOutputInvalid("model returned an empty response", raw=text)

    candidates: list[str] = []
    fenced = _strip_fences(text)
    if fenced != text.strip():
        candidates.append(fenced)
    candidates.append(text.strip())
    obj = _first_json_object(fenced) or _first_json_object(text)
    if obj:
        candidates.append(obj)

    for candidate in candidates:
        for variant in (candidate, _repair(candidate)):
            try:
                parsed = json.loads(variant)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise LLMOutputInvalid("model output did not contain a JSON object", raw=text[:4000])


def validate(model: type[T], payload: dict[str, Any]) -> T:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise LLMOutputInvalid(f"model output failed validation: {exc.errors()[:5]}") from exc


def parse(model: type[T], text: str) -> T:
    return validate(model, extract_json(text))
