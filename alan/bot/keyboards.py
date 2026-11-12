"""Inline keyboard construction."""

from __future__ import annotations

from typing import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..core.service import Button


def build(rows: Sequence[Sequence[Button]]) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    keyboard = [
        [InlineKeyboardButton(text=button.label, callback_data=button.data) for button in row]
        for row in rows
        if row
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None


def confirm_delete(kind: str, target_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Delete", callback_data=f"delete:{kind}:{target_id}"),
                InlineKeyboardButton(text="Cancel", callback_data="delete:cancel:0"),
            ]
        ]
    )
