"""Tests for the Telethon user-account sink, driven by a fake client.

No network, no real Telethon session — we inject a stand-in client that records
the calls the sink makes, and assert the mapping from OutboundMessage to
Telethon's send_message / send_file.
"""

import pytest

from bridge.adapters.telegram_user_sink import TelegramUserSink, _coerce_peer
from bridge.core.models import MessageKind, OutboundMessage, TelegramTarget


class FakeClient:
    def __init__(self, authorized=True):
        self._authorized = authorized
        self._connected = False
        self.messages = []
        self.files = []
        self.disconnected = False

    def is_connected(self):
        return self._connected

    async def connect(self):
        self._connected = True

    async def is_user_authorized(self):
        return self._authorized

    async def send_message(self, entity, text, **kw):
        self.messages.append((entity, text, kw))

    async def send_file(self, entity, file, **kw):
        self.files.append((entity, file, kw))

    async def disconnect(self):
        self.disconnected = True


def _sink(authorized=True):
    fake = FakeClient(authorized=authorized)
    return TelegramUserSink(0, "", client=fake), fake


@pytest.mark.parametrize(
    "raw,expected",
    [("123", 123), ("-1001234567890", -1001234567890),
     ("@channel", "@channel"), ("+15551234", "+15551234"),
     ("https://t.me/foo", "https://t.me/foo")],
)
def test_coerce_peer(raw, expected):
    assert _coerce_peer(raw) == expected


async def test_text_calls_send_message_literally():
    sink, fake = _sink()
    await sink.deliver(
        TelegramTarget(chat_id="-100999"),
        OutboundMessage(kind=MessageKind.TEXT, text="a*b_c"),
    )
    assert fake.messages[0][0] == -100999  # coerced to int
    assert fake.messages[0][1] == "a*b_c"
    # owner text is sent literally, no markdown/html reinterpretation
    assert fake.messages[0][2]["parse_mode"] is None


async def test_html_flag_enables_html_parse_mode():
    sink, fake = _sink()
    await sink.deliver(
        TelegramTarget(chat_id="1"),
        OutboundMessage(kind=MessageKind.TEXT, text="<b>x</b>", html=True),
    )
    assert fake.messages[0][2]["parse_mode"] == "html"


async def test_topic_thread_is_reply_to():
    sink, fake = _sink()
    await sink.deliver(
        TelegramTarget(chat_id="42", message_thread_id=7),
        OutboundMessage(kind=MessageKind.TEXT, text="hi"),
    )
    assert fake.messages[0][2]["reply_to"] == 7


async def test_image_calls_send_file_as_media():
    sink, fake = _sink()
    await sink.deliver(
        TelegramTarget(chat_id="@me"),
        OutboundMessage(
            kind=MessageKind.IMAGE, text="<b>Al</b>",
            media_bytes=b"PNG", filename="p.png", mimetype="image/png",
        ),
    )
    entity, stream, kw = fake.files[0]
    assert entity == "@me"
    assert stream.read() == b"PNG"
    assert stream.name == "p.png"
    assert kw["caption"] == "<b>Al</b>"
    assert kw["force_document"] is False  # image renders natively


async def test_sticker_and_file_force_document():
    for kind, fn in [(MessageKind.STICKER, "s.webp"), (MessageKind.FILE, "d.zip")]:
        sink, fake = _sink()
        await sink.deliver(
            TelegramTarget(chat_id="1"),
            OutboundMessage(kind=kind, media_bytes=b"X", filename=fn),
        )
        assert fake.files[0][2]["force_document"] is True


async def test_unauthorized_session_raises():
    sink, _ = _sink(authorized=False)
    with pytest.raises(RuntimeError, match="not authorized"):
        await sink.deliver(
            TelegramTarget(chat_id="1"),
            OutboundMessage(kind=MessageKind.TEXT, text="hi"),
        )
