"""Deterministic text utilities, independent of any model."""

from __future__ import annotations

from alan.textutil import (
    detect_language,
    human_datetime,
    parse_iso,
    slugify,
    title_from_text,
    clamp,
)


def test_language_detection_russian():
    assert detect_language("Мне нужно разобраться с учебным планом.") == "ru"


def test_language_detection_ukrainian():
    assert detect_language("Мені потрібно розібратися з навчальним планом, це є важливо.") == "uk"


def test_language_detection_english():
    assert detect_language("I need to figure out my learning plan.") == "en"


def test_language_detection_mixed_prefers_cyrillic():
    assert detect_language("Backend — основной приоритет.") == "ru"


def test_slugify_transliterates_cyrillic():
    assert slugify("Бэкенд против AI") == "bekend-protiv-ai"
    assert slugify("   ") == "note"


def test_title_from_text_trims_to_first_sentence():
    assert title_from_text("Backend stays primary. AI is parallel.") == "Backend stays primary"


def test_human_datetime_is_localised():
    stamp = "2026-11-11T14:24:00+00:00"
    assert human_datetime(stamp, "en") == "11 November, 14:24"
    assert human_datetime(stamp, "ru") == "11 ноября, 14:24"


def test_parse_iso_accepts_z_suffix():
    assert parse_iso("2026-11-11T14:24:00Z").hour == 14


def test_clamp_keeps_short_text():
    assert clamp("short", 20) == "short"
    assert len(clamp("x" * 50, 20)) == 20
