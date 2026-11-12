"""JSON recovery and validation: the layer that protects storage from models."""

from __future__ import annotations

import pytest

from alan.errors import LLMOutputInvalid
from alan.llm.jsonx import extract_json, parse
from alan.core.schemas import Assessment


def test_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_fenced_json():
    text = '```json\n{"a": 1}\n```'
    assert extract_json(text) == {"a": 1}


def test_prose_preamble_and_trailing_comma():
    text = 'Here is the result:\n{"a": 1, "b": [1, 2,],}\nHope that helps.'
    assert extract_json(text) == {"a": 1, "b": [1, 2]}


def test_curly_quotes_are_repaired():
    text = '{“a”: 1}'
    assert extract_json(text) == {"a": 1}


def test_missing_json_raises():
    with pytest.raises(LLMOutputInvalid):
        extract_json("I cannot help with that.")


def test_assessment_defaults_are_applied():
    payload = {
        "intent": "capture",
        "note": {"title": "T", "summary": "S", "constraints": "one constraint"},
    }
    assessment = parse(Assessment, __import__("json").dumps(payload))
    assert assessment.needs_clarification is False
    assert assessment.note.constraints == ["one constraint"]
    assert assessment.note.type is None


def test_invalid_basis_is_rejected_elsewhere_but_intent_is_constrained():
    with pytest.raises(LLMOutputInvalid):
        parse(Assessment, '{"intent": "teleport"}')
