"""Relay tests: TG -> Matrix, with DM-default filter, labels, mute, media."""

import pytest

from bridge.core.messagelinks import MessageLinks
from bridge.core.models import InboundMessage, MediaRef, MessageKind
from bridge.core.relay import Relay
from bridge.core.replymap import ReplyMap
from bridge.core.state import BridgeState

from .fakes import FakeFetcher, RecordingSink

ROOM = "!room:matrix.org"


def _build(muted=(), watched=()):
    mx = RecordingSink()
    fetcher = FakeFetcher()
    state = BridgeState()
    for m in muted:
        state.mute(m)
    for w in watched:
        state.watch(w)
    return Relay(mx, fetcher, state, ROOM), mx, state


def _dm(**kw):
    return InboundMessage(MessageKind.TEXT, "111", "Alice", source_kind="user", **kw)


async def test_dm_is_relayed_by_default():
    relay, mx, _ = _build()
    await relay.on_telegram_message(_dm(text="hi", source_label="Alice DM"))
    target, msg = mx.deliveries[0]
    assert target.chat_id == ROOM
    assert msg.text == "<b>[Alice DM]</b> <b>Alice</b>: hi"
    assert msg.silent is False


async def test_channel_not_relayed_unless_watched():
    relay, mx, _ = _build()
    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "-100999", "Bot", text="spam",
                       source_kind="channel", source_label="News")
    )
    assert mx.deliveries == []  # filtered out


async def test_channel_relayed_when_watched():
    relay, mx, _ = _build(watched=["-100999"])
    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "-100999", "Bot", text="story",
                       source_kind="channel", source_label="News")
    )
    assert mx.deliveries[0][1].text == "<b>[News]</b> <b>Bot</b>: story"


async def test_group_not_relayed_unless_watched():
    relay, mx, _ = _build()
    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "-333", "Al", text="hey",
                       source_kind="group", source_label="My Group")
    )
    assert mx.deliveries == []


async def test_muted_dm_is_silent():
    relay, mx, _ = _build(muted=["111"])
    await relay.on_telegram_message(_dm(text="hi", source_label="Alice"))
    assert mx.deliveries[0][1].silent is True


async def test_filtered_channel_skips_media_download():
    relay, mx, state = _build()  # not watched
    fetcher = relay._fetcher
    ref = MediaRef(uri="-100999:5", mimetype="image/jpeg", filename="pic.jpg")
    await relay.on_telegram_message(
        InboundMessage(MessageKind.IMAGE, "-100999", "Bob", media=ref,
                       source_kind="channel", source_label="News")
    )
    assert mx.deliveries == []
    assert fetcher.calls == []  # no wasted download for a filtered source


async def test_relay_records_reply_mapping():
    mx = RecordingSink(return_id="$evt1")   # Matrix event id of the posted line
    rm = ReplyMap()
    relay = Relay(mx, FakeFetcher(), BridgeState(), ROOM, reply_map=rm)
    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "111", "Alice", text="hi",
                       source_kind="user", source_label="Alice", source_msg_id=42)
    )
    ref = rm.lookup("$evt1")
    assert ref is not None
    assert ref.chat_id == 111 and ref.msg_id == 42 and ref.kind == "user"


# -- reply threading (TG -> Matrix) --------------------------------------------


def _group_msg(**kw):
    return InboundMessage(MessageKind.TEXT, "-333", "Bob", source_kind="group",
                          source_label="Group", **kw)


async def _relay_with_links(mx, links):
    state = BridgeState()
    state.watch("-333")
    return Relay(mx, FakeFetcher(), state, ROOM, links=links)


async def test_reply_between_others_threads_in_matrix():
    """Bob replying to Alice in a TG group must be a Matrix reply too."""
    links = MessageLinks()
    links.add(-333, 10, "group", ROOM, "$alice")
    mx = RecordingSink(return_id="$bob")
    relay = await _relay_with_links(mx, links)

    await relay.on_telegram_message(
        _group_msg(text="agreed", source_msg_id=11, reply_to_msg_id=10)
    )

    assert mx.deliveries[0][1].reply_to_event == "$alice"


async def test_reply_to_unlinked_message_still_relays():
    links = MessageLinks()
    mx = RecordingSink(return_id="$bob")
    relay = await _relay_with_links(mx, links)

    await relay.on_telegram_message(
        _group_msg(text="agreed", source_msg_id=11, reply_to_msg_id=10)
    )

    msg = mx.deliveries[0][1]
    assert msg.text.endswith("agreed") and msg.reply_to_event is None


async def test_reply_target_in_another_room_is_not_used():
    """A dangling event id from a different room would render as a broken
    reply, so it is dropped rather than sent."""
    links = MessageLinks()
    links.add(-333, 10, "group", "!elsewhere:hs", "$alice")
    mx = RecordingSink(return_id="$bob")
    relay = await _relay_with_links(mx, links)

    await relay.on_telegram_message(
        _group_msg(text="agreed", source_msg_id=11, reply_to_msg_id=10)
    )

    assert mx.deliveries[0][1].reply_to_event is None


async def test_reply_target_is_not_matched_across_chats():
    """Message id 10 in another chat must not become the reply target."""
    links = MessageLinks()
    links.add(111, 10, "user", ROOM, "$other_chat")
    mx = RecordingSink(return_id="$bob")
    relay = await _relay_with_links(mx, links)

    await relay.on_telegram_message(
        _group_msg(text="agreed", source_msg_id=11, reply_to_msg_id=10)
    )

    assert mx.deliveries[0][1].reply_to_event is None


async def test_reply_chain_survives_relayed_replies():
    """The relayed reply is itself linked, so a reply to it threads as well."""
    links = MessageLinks()
    links.add(-333, 10, "group", ROOM, "$alice")
    mx = RecordingSink(return_id="$bob")
    relay = await _relay_with_links(mx, links)

    await relay.on_telegram_message(
        _group_msg(text="agreed", source_msg_id=11, reply_to_msg_id=10)
    )
    await relay.on_telegram_message(
        _group_msg(text="me too", source_msg_id=12, reply_to_msg_id=11)
    )

    assert mx.deliveries[1][1].reply_to_event == "$bob"


# -- your own messages ---------------------------------------------------------


async def test_own_message_is_relayed_too():
    """Otherwise a room shows only the other half of the conversation.

    This is what made a shared group look like it "sometimes" failed to sync:
    with two accounts in one group, a message sent by account A is outgoing for
    A (dropped) and incoming for B (relayed).
    """
    relay, mx, _ = _build()
    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "111", "Me", text="on my way",
                       source_kind="user", source_label="Alice", outgoing=True)
    )
    assert "on my way" in mx.deliveries[0][1].text


async def test_own_message_is_marked_as_mine():
    relay, mx, _ = _build()
    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "111", "Me", text="hi",
                       source_kind="user", source_label="Alice", outgoing=True)
    )
    assert "我" in mx.deliveries[0][1].text


async def test_own_message_in_a_dm_room_is_still_attributed():
    """A DM room shows no sender, so without a marker both sides look alike."""
    from bridge.core.roomregistry import RoomRegistry

    from .fakes import FakeRoomCreator

    reg = RoomRegistry()
    reg.register(111, "!dm:hs", "Alice")
    mx = RecordingSink(return_id="$e")
    relay = Relay(mx, FakeFetcher(), BridgeState(), ROOM, registry=reg,
                  rooms=FakeRoomCreator())

    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "111", "Me", text="mine",
                       source_kind="user", outgoing=True)
    )
    body = mx.deliveries[0][1].text
    assert body.startswith("<b>我</b>") and "mine" in body


async def test_the_bridges_own_send_is_not_echoed_back():
    """Telegram reports it as a new outgoing message; Matrix already has it."""
    links = MessageLinks()
    links.add(111, 42, "user", ROOM, "$typed-in-matrix")
    mx = RecordingSink(return_id="$e")
    relay = Relay(mx, FakeFetcher(), BridgeState(), ROOM, links=links)

    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "111", "Me", text="sent from matrix",
                       source_kind="user", source_msg_id=42, outgoing=True)
    )

    assert mx.deliveries == []


async def test_an_unlinked_own_message_still_relays():
    """Sent from the phone, not through the bridge — it belongs in the room."""
    links = MessageLinks()
    mx = RecordingSink(return_id="$e")
    relay = Relay(mx, FakeFetcher(), BridgeState(), ROOM, links=links)

    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "111", "Me", text="from my phone",
                       source_kind="user", source_msg_id=43, outgoing=True)
    )

    assert "from my phone" in mx.deliveries[0][1].text


async def test_incoming_messages_are_unaffected_by_the_echo_guard():
    links = MessageLinks()
    links.add(111, 42, "user", ROOM, "$e")
    mx = RecordingSink(return_id="$e2")
    relay = Relay(mx, FakeFetcher(), BridgeState(), ROOM, links=links)

    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "111", "Alice", text="theirs",
                       source_kind="user", source_msg_id=42)
    )

    assert "theirs" in mx.deliveries[0][1].text


async def test_own_message_in_an_unwatched_group_is_still_filtered():
    relay, mx, _ = _build()  # not watched, no room
    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "-100999", "Me", text="mine",
                       source_kind="channel", source_label="News", outgoing=True)
    )
    assert mx.deliveries == []


# -- stickers ------------------------------------------------------------------


async def test_sticker_relays_as_text_not_an_image():
    """.webp/.tgs render as a broken image in most clients, if at all."""
    relay, mx, _ = _build()
    ref = MediaRef(uri="111:5", mimetype="image/webp", filename="sticker.webp")
    await relay.on_telegram_message(
        InboundMessage(MessageKind.STICKER, "111", "Alice", media=ref,
                       source_kind="user", source_label="Alice")
    )
    msg = mx.deliveries[0][1]
    assert msg.kind is MessageKind.TEXT and msg.media_bytes is None
    assert "【sticker】" in msg.text


async def test_sticker_download_is_skipped_entirely():
    relay, _mx, _ = _build()
    ref = MediaRef(uri="111:5", mimetype="image/webp")
    await relay.on_telegram_message(
        InboundMessage(MessageKind.STICKER, "111", "Alice", media=ref,
                       source_kind="user", source_label="Alice")
    )
    assert relay._fetcher.calls == []  # no wasted download


async def test_sticker_shows_its_emoji_when_known():
    relay, mx, _ = _build()
    await relay.on_telegram_message(
        InboundMessage(MessageKind.STICKER, "111", "Alice", text="😀",
                       source_kind="user", source_label="Alice")
    )
    assert "【sticker】😀" in mx.deliveries[0][1].text


async def test_sticker_link_records_the_rendered_text():
    """So a later delete/edit re-renders the line the room actually shows."""
    links = MessageLinks()
    mx = RecordingSink(return_id="$s")
    relay = Relay(mx, FakeFetcher(), BridgeState(), ROOM, links=links)
    await relay.on_telegram_message(
        InboundMessage(MessageKind.STICKER, "111", "Alice", text="😀",
                       source_kind="user", source_msg_id=7)
    )
    assert links.get(111, 7).text == "【sticker】😀"


async def test_watched_media_is_fetched_and_forwarded():
    relay, mx, _ = _build(watched=["-333"])
    ref = MediaRef(uri="-333:5", mimetype="image/jpeg", filename="pic.jpg")
    await relay.on_telegram_message(
        InboundMessage(MessageKind.IMAGE, "-333", "Bob", media=ref,
                       source_kind="group", source_label="Group")
    )
    msg = mx.deliveries[0][1]
    assert msg.kind is MessageKind.IMAGE
    assert msg.media_bytes == b"BYTES"
    assert msg.text == "<b>[Group]</b> <b>Bob</b>"


# -- forwarded messages ------------------------------------------------------


async def test_forward_is_marked_with_its_origin():
    """The words are someone else's; the relay must say whose."""
    relay, mx, _ = _build()
    await relay.on_telegram_message(
        _dm(text="转来的内容", source_label="Alice", forward_from="Carol")
    )
    body = mx.deliveries[0][1].text
    assert "↪️ 转发自 Carol" in body and "转来的内容" in body


async def test_a_normal_message_carries_no_forward_marker():
    relay, mx, _ = _build()
    await relay.on_telegram_message(_dm(text="hi", source_label="Alice"))
    assert "转发自" not in mx.deliveries[0][1].text


async def test_forward_marker_survives_a_dm_room_with_no_sender_head():
    """A DM's own room prints no sender, so the marker is the whole prefix."""
    from bridge.core.roomregistry import RoomRegistry

    from .fakes import FakeRoomCreator

    reg = RoomRegistry()
    reg.register(111, "!dm:hs", "Alice")
    mx = RecordingSink(return_id="$e")
    relay = Relay(mx, FakeFetcher(), BridgeState(), ROOM, registry=reg,
                  rooms=FakeRoomCreator())

    await relay.on_telegram_message(_dm(text="转来的", forward_from="某频道"))
    body = mx.deliveries[0][1].text
    assert body.startswith("<i>↪️ 转发自 某频道</i>") and "转来的" in body


async def test_forwarded_media_is_marked_too():
    """The caption carries the head, so a forwarded photo is attributed too."""
    relay, mx, _ = _build()
    await relay.on_telegram_message(
        InboundMessage(MessageKind.IMAGE, "111", "Alice", text="看图",
                       source_kind="user", source_label="Alice",
                       media=MediaRef(uri="111:9"), forward_from="李四")
    )
    assert "↪️ 转发自 李四" in mx.deliveries[0][1].text


class _Editor:
    def __init__(self):
        self.replaced = []   # (room, event, html)
        self.annotated = []  # (room, event, html)

    async def replace_event(self, room_id, event_id, html):
        self.replaced.append((room_id, event_id, html))

    async def annotate_event(self, room_id, event_id, html):
        self.annotated.append((room_id, event_id, html))


async def test_an_edited_forward_keeps_its_marker():
    """Edits re-render from the stored head, which carries the marker."""
    links = MessageLinks()
    mx = RecordingSink(return_id="$e1")
    editor = _Editor()
    relay = Relay(mx, FakeFetcher(), BridgeState(), ROOM, links=links,
                  editor=editor)

    await relay.on_telegram_message(
        _dm(text="原文", source_label="Alice", source_msg_id=5,
            forward_from="李四")
    )
    await relay.on_telegram_edited(111, 5, "改后")
    assert "↪️ 转发自 李四" in editor.replaced[0][2]
