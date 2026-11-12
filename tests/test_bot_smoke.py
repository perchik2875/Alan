"""The bot layer must import and assemble without a live Telegram connection."""

from __future__ import annotations

from alan.bot import handlers, keyboards, views
from alan.bot.app import build_dispatcher
from alan.core.service import Button, TurnResult


def test_dispatcher_assembles(service, cfg):
    dp = build_dispatcher(service, cfg)
    assert dp is not None
    assert dp.sub_routers


def test_views_chunk_never_exceeds_limit():
    text = "\n".join("line " + str(i) for i in range(2000))
    chunks = views.chunk(text, limit=500)
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "".join(chunks).replace(" ", "").startswith("line0")


def test_keyboard_builder_skips_empty_rows():
    markup = keyboards.build([[Button(label="A", data="a:b:c")], []])
    assert markup is not None
    assert len(markup.inline_keyboard) == 1
    assert keyboards.build([]) is None


def test_note_card_is_plain_text_safe():
    from alan.models import Note

    note = Note(
        id="n_1", slug="demo", title="*bold* _text_", type="fact", status="active",
        project_id=None, language="en", summary="<script>", content={},
        body_md="", path="notes/demo.md", version=1, sensitive=False, indexed=True,
        superseded_by=None, created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    card = views.note_card(note)
    assert "*bold*" in card and "<script>" in card


async def test_send_result_chunks_long_text():
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
            sent.append(text)

    result = TurnResult(text="x" * 9000, buttons=[])
    await handlers.send_result(FakeBot(), 1, result)
    assert len(sent) == 3
