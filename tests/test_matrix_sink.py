"""MatrixSink tests with a fake nio client (no homeserver)."""

import pytest

from bridge.adapters.matrix_sink import MatrixSink
from bridge.adapters.matrix_source import BRIDGE_ORIGIN_KEY
from bridge.core.models import MessageKind, OutboundMessage, TelegramTarget

ROOM = "!room:matrix.org"


class FakeUploadResp:
    content_uri = "mxc://server/media123"


class FakeMxClient:
    def __init__(self):
        self.sent = []
        self.uploaded = None

    async def room_send(self, room_id, event_type, content):
        self.sent.append((room_id, event_type, content))

    async def upload(self, provider, content_type, filename, filesize):
        self.uploaded = (content_type, filename, filesize)
        return FakeUploadResp(), None


async def test_text_is_m_text_with_flag_and_html():
    c = FakeMxClient()
    sink = MatrixSink(c)
    await sink.deliver(
        TelegramTarget(chat_id=ROOM),
        OutboundMessage(kind=MessageKind.TEXT, text="<b>hi</b>", html=True),
    )
    _, etype, content = c.sent[0]
    assert etype == "m.room.message"
    assert content["msgtype"] == "m.text"
    assert content["formatted_body"] == "<b>hi</b>"
    assert content["body"] == "hi"                 # plain fallback
    assert content[BRIDGE_ORIGIN_KEY] == "telegram"  # loop guard stamp


async def test_silent_is_m_notice():
    c = FakeMxClient()
    await MatrixSink(c).deliver(
        TelegramTarget(chat_id=ROOM),
        OutboundMessage(kind=MessageKind.TEXT, text="x", html=True, silent=True),
    )
    assert c.sent[0][2]["msgtype"] == "m.notice"


async def test_reply_event_becomes_an_in_reply_to_relation():
    c = FakeMxClient()
    await MatrixSink(c).deliver(
        TelegramTarget(chat_id=ROOM),
        OutboundMessage(kind=MessageKind.TEXT, text="re", html=True,
                        reply_to_event="$orig"),
    )
    rel = c.sent[0][2]["m.relates_to"]
    assert rel == {"m.in_reply_to": {"event_id": "$orig"}}


async def test_plain_message_has_no_relation():
    c = FakeMxClient()
    await MatrixSink(c).deliver(
        TelegramTarget(chat_id=ROOM),
        OutboundMessage(kind=MessageKind.TEXT, text="hi", html=True),
    )
    assert "m.relates_to" not in c.sent[0][2]


async def test_media_reply_relates_to_the_original():
    c = FakeMxClient()
    await MatrixSink(c).deliver(
        TelegramTarget(chat_id=ROOM),
        OutboundMessage(kind=MessageKind.IMAGE, media_bytes=b"JPG",
                        filename="p.jpg", mimetype="image/jpeg",
                        reply_to_event="$orig"),
    )
    assert c.sent[0][2]["m.relates_to"]["m.in_reply_to"]["event_id"] == "$orig"


async def test_image_uploads_and_sends_media_then_caption():
    c = FakeMxClient()
    await MatrixSink(c).deliver(
        TelegramTarget(chat_id=ROOM),
        OutboundMessage(
            kind=MessageKind.IMAGE,
            text="<b>[G]</b> <b>Al</b>",
            media_bytes=b"JPG",
            filename="p.jpg",
            mimetype="image/jpeg",
            html=True,
        ),
    )
    assert c.uploaded == ("image/jpeg", "p.jpg", 3)
    media = c.sent[0][2]
    assert media["msgtype"] == "m.image"
    assert media["url"] == "mxc://server/media123"
    assert media[BRIDGE_ORIGIN_KEY] == "telegram"
    # caption line follows the media
    assert c.sent[1][2]["formatted_body"] == "<b>[G]</b> <b>Al</b>"
