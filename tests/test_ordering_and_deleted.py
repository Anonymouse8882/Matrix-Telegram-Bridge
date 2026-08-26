"""Two things the bridge must not get wrong.

*Order*: Telethon dispatches every update as its own task, so a message that
has to download media would land in Matrix after a later plain-text one. The
source serialises per chat to put that right.

*Deleted chats*: a room whose Telegram side is gone keeps its history, but is
renamed to say so — a room that can never receive another message must not
look like a merely quiet one.
"""

import asyncio

from types import SimpleNamespace

from bridge.core.dispatcher import Dispatcher
from bridge.core.models import Dialog, InboundMessage, MessageKind
from bridge.core.relay import Relay
from bridge.core.roomregistry import RoomRegistry
from bridge.core.state import BridgeState

from .fakes import (
    FakeAccounts,
    FakeDirectory,
    FakeFetcher,
    FakeRoomCreator,
    FakeSender,
    RecordingSink,
)

CONTROL = "!control:matrix.org"


# -- per-chat ordering ---------------------------------------------------------


class _FakeTgClient:
    """Just enough Telethon for the source's queueing to be exercised."""

    def __init__(self):
        self.handlers = []

    def add_event_handler(self, callback, event):
        self.handlers.append(callback)

    def is_connected(self):
        return False


def _event(chat_id, msg_id, text):
    """A Telethon-ish NewMessage event whose normalisation we control."""
    message = SimpleNamespace(
        id=msg_id, message=text, media=None, file=None, reply_to=None,
        out=False, sticker=None, photo=None, video=None, video_note=None,
        gif=None, voice=None, audio=None, document=None, post_author=None,
    )
    return SimpleNamespace(
        chat_id=chat_id, message=message, id=msg_id,
        is_private=True, is_channel=False, is_group=False,
    )


def _source_with(slow_ids: set, delivered: list):
    """A source whose normalisation stalls for `slow_ids` (like a media
    download would), so ordering is actually put under stress."""
    from bridge.adapters.telegram_user_source import TelegramUserSource

    src = TelegramUserSource(_FakeTgClient())

    async def fake_to_inbound(event):
        if event.id in slow_ids:
            await asyncio.sleep(0.05)  # the download that used to jump the queue
        return InboundMessage(
            MessageKind.TEXT, str(event.chat_id), "X",
            text=event.message.message, source_kind="user",
            source_msg_id=event.id,
        )

    async def handler(msg):
        delivered.append((msg.source_room, msg.source_msg_id))

    src._to_inbound = fake_to_inbound
    src.set_handler(handler)
    return src


async def test_messages_in_one_chat_keep_their_order():
    """The regression: msg 1 carries media, msg 2 is text — 2 used to win."""
    delivered = []
    src = _source_with({1}, delivered)

    await src._on_new_message(_event(111, 1, "slow with photo"))
    await src._on_new_message(_event(111, 2, "fast text"))
    await asyncio.sleep(0.2)

    assert delivered == [("111", 1), ("111", 2)]


async def test_a_long_run_stays_in_order():
    delivered = []
    src = _source_with({2, 5, 7}, delivered)

    for i in range(1, 10):
        await src._on_new_message(_event(111, i, f"m{i}"))
    await asyncio.sleep(0.4)

    assert delivered == [("111", i) for i in range(1, 10)]


async def test_a_slow_chat_does_not_hold_up_another():
    """Ordering is per chat, not global: one busy chat must not stall the rest,
    which is why this is a queue per chat and not `sequential_updates`."""
    delivered = []
    src = _source_with({1}, delivered)

    await src._on_new_message(_event(111, 1, "slow"))
    await src._on_new_message(_event(222, 2, "other chat"))
    await asyncio.sleep(0.02)  # before the slow one can finish

    assert delivered == [("222", 2)]


async def test_a_failing_message_does_not_stall_the_chat():
    delivered = []
    src = _source_with(set(), delivered)
    boom = {"hit": False}
    original = src._to_inbound

    async def sometimes_fails(event):
        if event.id == 1:
            boom["hit"] = True
            raise RuntimeError("bad event (test)")
        return await original(event)

    src._to_inbound = sometimes_fails
    await src._on_new_message(_event(111, 1, "bad"))
    await src._on_new_message(_event(111, 2, "good"))
    await asyncio.sleep(0.1)

    assert boom["hit"] and delivered == [("111", 2)]


# -- marking a deleted chat ----------------------------------------------------

DIALOGS = [Dialog(id=111, name="小明", kind="user", username="qiuqiu")]


def _dispatcher(registry, rooms, directory=None):
    directory = directory or FakeDirectory(DIALOGS)
    mx = RecordingSink()
    accounts = FakeAccounts(directory, FakeSender(), registry=registry, rooms=rooms)
    d = Dispatcher(accounts, mx, BridgeState(), CONTROL,
                   command_prefix="!tg", timezone="UTC")
    return d, mx, directory


def _mx(text, room=CONTROL):
    return InboundMessage(kind=MessageKind.TEXT, source_room=room,
                          sender="@me", text=text, event_id="$e1")


async def test_check_marks_a_room_whose_chat_is_gone():
    reg, creator = RoomRegistry(), FakeRoomCreator()
    reg.register(111, "!r1:hs", "小明", kind="user")
    directory = FakeDirectory(DIALOGS)
    directory.presences[111] = "gone"
    d, mx, _ = _dispatcher(reg, creator, directory)

    await d.on_matrix_message(_mx("!tg check"))

    assert creator.names == [("!r1:hs", "🗑 小明（已删除）")]
    assert reg.is_deleted(111)
    assert "已标记为删除" in mx.deliveries[-1][1].text


async def test_check_reports_a_deleted_account_differently():
    reg, creator = RoomRegistry(), FakeRoomCreator()
    reg.register(111, "!r1:hs", "小明", kind="user")
    directory = FakeDirectory(DIALOGS)
    directory.presences[111] = "deleted"
    d, mx, _ = _dispatcher(reg, creator, directory)

    await d.on_matrix_message(_mx("!tg check"))

    assert "账户已注销" in mx.deliveries[-1][1].text


async def test_check_leaves_a_live_chat_alone():
    reg, creator = RoomRegistry(), FakeRoomCreator()
    reg.register(111, "!r1:hs", "小明", kind="user")
    d, mx, _ = _dispatcher(reg, creator)

    await d.on_matrix_message(_mx("!tg check"))

    assert creator.names == []
    assert not reg.is_deleted(111)
    assert "没有发现已删除" in mx.deliveries[-1][1].text


async def test_check_does_not_rename_twice():
    """Re-running the sweep must not keep rewriting the same name."""
    reg, creator = RoomRegistry(), FakeRoomCreator()
    reg.register(111, "!r1:hs", "小明", kind="user")
    directory = FakeDirectory(DIALOGS)
    directory.presences[111] = "gone"
    d, _mx_sink, _ = _dispatcher(reg, creator, directory)

    await d.on_matrix_message(_mx("!tg check"))
    await d.on_matrix_message(_mx("!tg check"))

    assert len(creator.names) == 1


async def test_check_restores_a_chat_that_came_back():
    reg, creator = RoomRegistry(), FakeRoomCreator()
    reg.register(111, "!r1:hs", "小明", kind="user")
    reg.set_deleted(111, True)
    d, mx, _ = _dispatcher(reg, creator)  # presence defaults to "ok"

    await d.on_matrix_message(_mx("!tg check"))

    assert creator.names == [("!r1:hs", "小明")]
    assert not reg.is_deleted(111)
    assert "恢复正常" in mx.deliveries[-1][1].text


async def test_a_message_from_a_marked_chat_restores_the_name():
    """A live message is the one unambiguous proof the chat is not deleted."""
    reg, creator = RoomRegistry(), FakeRoomCreator()
    reg.register(111, "!r1:hs", "小明", kind="user")
    reg.set_deleted(111, True)
    mx = RecordingSink(return_id="$e")
    relay = Relay(mx, FakeFetcher(), BridgeState(), CONTROL,
                  registry=reg, rooms=creator)

    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "111", "小明", text="在吗",
                       source_kind="user", source_label="小明")
    )

    assert creator.names == [("!r1:hs", "小明")]
    assert not reg.is_deleted(111)
    assert "在吗" in mx.deliveries[0][1].text  # and the message still relays


async def test_the_deleted_mark_survives_a_restart(tmp_path):
    path = str(tmp_path / "rooms.json")
    reg = RoomRegistry(path)
    reg.register(111, "!r1:hs", "小明", kind="user")
    reg.set_deleted(111, True)

    assert RoomRegistry(path).is_deleted(111)
