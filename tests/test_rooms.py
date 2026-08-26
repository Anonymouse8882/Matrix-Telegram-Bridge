"""Per-chat room feature: registry, lazy creation in the relay, and routing
messages typed in a per-chat room back to its Telegram chat."""

import pytest

from bridge.core.dispatcher import Dispatcher
from bridge.core.models import Dialog, InboundMessage, MessageKind
from bridge.core.relay import Relay
from bridge.core.replymap import ReplyMap, ReplyRef
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

DIALOGS = [
    Dialog(id=111, name="Alice", kind="user", username="alice"),
    Dialog(id=-100222, name="News", kind="channel", username="news"),
]


# -- registry ------------------------------------------------------------------


def test_registry_round_trip(tmp_path):
    path = str(tmp_path / "rooms.json")
    reg = RoomRegistry(path)
    reg.register(111, "!r1:hs", "Alice")

    fresh = RoomRegistry(path)
    assert fresh.room_for(111) == "!r1:hs"
    assert fresh.chat_for("!r1:hs") == 111
    assert fresh.name_for(111) == "Alice"
    assert fresh.rooms() == {"!r1:hs"}


def test_registry_remap_drops_stale_reverse_mapping():
    reg = RoomRegistry()
    reg.register(111, "!old:hs")
    reg.register(111, "!new:hs")
    assert reg.chat_for("!old:hs") is None
    assert reg.chat_for("!new:hs") == 111


def test_registry_corrupt_file_degrades_to_empty(tmp_path):
    path = tmp_path / "rooms.json"
    path.write_text("{broken", encoding="utf-8")
    reg = RoomRegistry(str(path))
    assert reg.rooms() == set()


def test_registry_remembers_the_chat_kind(tmp_path):
    path = str(tmp_path / "rooms.json")
    reg = RoomRegistry(path)
    reg.register(-100222, "!r:hs", "News", kind="channel")
    assert RoomRegistry(path).kind_for(-100222) == "channel"


def test_registry_kind_defaults_to_empty_for_old_mappings():
    reg = RoomRegistry()
    reg.register(111, "!r:hs", "Alice")  # pre-kind mapping
    assert reg.kind_for(111) == ""


# -- relay: lazy creation ------------------------------------------------------


def _tg(chat=111, kind="user", text="hi", label="Alice"):
    return InboundMessage(
        kind=MessageKind.TEXT, source_room=str(chat), sender=label,
        text=text, source_label=label, source_kind=kind, source_msg_id=7,
    )


def _relay(registry=None, rooms=None, watched=(), on_new_room=None,
           reply_map=None):
    sink = RecordingSink(return_id="$ev1")
    state = BridgeState()
    for w in watched:
        state.watch(w)
    relay = Relay(
        matrix_sink=sink, telegram_fetcher=FakeFetcher(), state=state,
        control_room=CONTROL, reply_map=reply_map,
        registry=registry, rooms=rooms, on_new_room=on_new_room,
    )
    return relay, sink


async def test_first_message_creates_room_and_delivers_there():
    reg, creator = RoomRegistry(), FakeRoomCreator()
    watched_rooms = []
    relay, sink = _relay(reg, creator, on_new_room=watched_rooms.append)

    await relay.on_telegram_message(_tg())

    assert creator.created[0].id == 111
    assert reg.room_for(111) == "!room1:test"
    assert sink.deliveries[0][0].chat_id == "!room1:test"
    assert watched_rooms == ["!room1:test"]  # source told to watch it


async def test_second_message_reuses_the_room():
    reg, creator = RoomRegistry(), FakeRoomCreator()
    relay, sink = _relay(reg, creator)

    await relay.on_telegram_message(_tg())
    await relay.on_telegram_message(_tg(text="again"))

    assert len(creator.created) == 1
    assert [d[0].chat_id for d in sink.deliveries] == ["!room1:test"] * 2


async def test_creation_failure_falls_back_to_control_room():
    """Rate limits etc. must degrade the layout, never lose the message."""
    reg = RoomRegistry()
    relay, sink = _relay(reg, FakeRoomCreator(fail=True))

    await relay.on_telegram_message(_tg())

    assert sink.deliveries[0][0].chat_id == CONTROL
    assert reg.room_for(111) is None  # not registered - next message retries


async def test_no_space_configured_keeps_old_behaviour():
    relay, sink = _relay(registry=None, rooms=None)
    await relay.on_telegram_message(_tg())
    assert sink.deliveries[0][0].chat_id == CONTROL


async def test_dedicated_dm_room_drops_label_and_sender():
    """In a DM's own room, '[Alice] Alice:' prefixes are pure noise."""
    relay, sink = _relay(RoomRegistry(), FakeRoomCreator())
    await relay.on_telegram_message(_tg(text="see you"))
    body = sink.deliveries[0][1].text
    assert body == "see you"


async def test_dedicated_group_room_keeps_sender():
    reg, creator = RoomRegistry(), FakeRoomCreator()
    relay, sink = _relay(reg, creator, watched=["-100222"])
    await relay.on_telegram_message(
        _tg(chat=-100222, kind="channel", label="News", text="hot")
    )
    body = sink.deliveries[0][1].text
    assert "News" in body and "hot" in body


async def test_control_room_message_keeps_full_label():
    relay, sink = _relay(registry=None, rooms=None)
    await relay.on_telegram_message(_tg(text="hello"))
    body = sink.deliveries[0][1].text
    assert "[Alice]" in body


async def test_reply_map_still_populated_in_dedicated_room():
    rm = ReplyMap()
    relay, _sink = _relay(RoomRegistry(), FakeRoomCreator(), reply_map=rm)
    await relay.on_telegram_message(_tg())
    ref = rm.lookup("$ev1")
    assert ref is not None and ref.chat_id == 111 and ref.msg_id == 7


async def test_concurrent_first_messages_create_only_one_room():
    """Telegram dispatches updates concurrently; the first two messages of a
    new chat must not each create a room."""
    import asyncio

    reg, creator = RoomRegistry(), FakeRoomCreator()
    relay, sink = _relay(reg, creator)

    await asyncio.gather(
        relay.on_telegram_message(_tg()),
        relay.on_telegram_message(_tg(text="second")),
    )

    assert len(creator.created) == 1
    assert {d[0].chat_id for d in sink.deliveries} == {"!room1:test"}


# -- dispatcher: typing in a per-chat room ------------------------------------


def _dispatcher(reg, rooms=None, reply_map=None, on_new_room=None):
    sender, mx = FakeSender(), RecordingSink()
    directory = FakeDirectory(DIALOGS)
    accounts = FakeAccounts(directory, sender, registry=reg, rooms=rooms,
                            reply_map=reply_map)
    d = Dispatcher(
        accounts, mx, BridgeState(), CONTROL,
        command_prefix="!tg", timezone="UTC", on_new_room=on_new_room,
    )
    return d, sender, mx


def _mx(room, text, reply_to=None, event="$e1"):
    return InboundMessage(
        kind=MessageKind.TEXT, source_room=room, sender="@me",
        text=text, reply_to_event=reply_to, event_id=event,
    )


async def test_typing_in_chat_room_sends_to_its_chat():
    reg = RoomRegistry()
    reg.register(111, "!r1:hs", "Alice")
    d, sender, _mx_sink = _dispatcher(reg)

    await d.on_matrix_message(_mx("!r1:hs", "hello there"))

    chat_id, kind, message, _at = sender.submissions[0]
    assert chat_id == 111 and kind == "user"
    assert message.text == "hello there"
    assert sender.origin_rooms[0] == "!r1:hs"  # self-destruct redacts HERE


async def test_typing_in_chat_room_uses_cached_kind_without_a_lookup():
    """With the kind recorded at registration, routing a typed message needs
    no per-message directory lookup (which used to cost a network round-trip,
    worst case a full dialog scan, for every message)."""
    reg = RoomRegistry()
    reg.register(-100222, "!r2:hs", "News", kind="channel")
    d, sender, _ = _dispatcher(reg)
    directory = d._accounts.current().directory

    await d.on_matrix_message(_mx("!r2:hs", "hello"))

    assert directory.resolve_calls == []
    _cid, kind, _msg, _at = sender.submissions[0]
    assert kind == "channel"


async def test_legacy_mapping_backfills_kind_once():
    """A pre-kind mapping resolves once, then the registry answers from cache."""
    reg = RoomRegistry()
    reg.register(111, "!r1:hs", "Alice")  # no kind recorded
    d, sender, _ = _dispatcher(reg)
    directory = d._accounts.current().directory

    await d.on_matrix_message(_mx("!r1:hs", "one"))
    await d.on_matrix_message(_mx("!r1:hs", "two"))

    assert len(directory.resolve_calls) == 1  # only the first message looks up
    assert reg.kind_for(111) == "user"
    assert [s[1] for s in sender.submissions] == ["user", "user"]


async def test_unmapped_room_is_ignored():
    d, sender, mx = _dispatcher(RoomRegistry())
    await d.on_matrix_message(_mx("!stranger:hs", "hello"))
    assert sender.submissions == [] and mx.deliveries == []


async def test_command_in_chat_room_is_intercepted_not_sent():
    """'!tg mute' typed in a chat room must never reach the human on TG."""
    reg = RoomRegistry()
    reg.register(111, "!r1:hs", "Alice")
    d, sender, mx = _dispatcher(reg)

    await d.on_matrix_message(_mx("!r1:hs", "!tg mute alice"))

    assert sender.submissions == []
    room, msg = mx.deliveries[0][0].chat_id, mx.deliveries[0][1].text
    assert room == "!r1:hs" and "控制房间" in msg


async def test_reply_in_chat_room_threads_back():
    rm = ReplyMap()
    rm.remember("$orig", ReplyRef(chat_id=111, msg_id=42, kind="user", name="Alice"))
    reg = RoomRegistry()
    reg.register(111, "!r1:hs", "Alice")
    d, sender, _ = _dispatcher(reg, reply_map=rm)

    await d.on_matrix_message(_mx("!r1:hs", "agreed", reply_to="$orig"))

    _cid, _k, message, _at = sender.submissions[0]
    assert message.reply_to == 42


async def test_cross_room_reply_ref_is_not_used():
    """A reply mapping for a DIFFERENT chat must not leak into this room."""
    rm = ReplyMap()
    rm.remember("$orig", ReplyRef(chat_id=999, msg_id=42, kind="user", name="Other"))
    reg = RoomRegistry()
    reg.register(111, "!r1:hs", "Alice")
    d, sender, _ = _dispatcher(reg, reply_map=rm)

    await d.on_matrix_message(_mx("!r1:hs", "agreed", reply_to="$orig"))

    _cid, _k, message, _at = sender.submissions[0]
    assert message.reply_to is None  # sent as a plain message instead


async def test_dedicated_room_relays_without_watch():
    """Creating a room IS the opt-in — an unwatched group with a room relays."""
    reg = RoomRegistry()
    reg.register(-100222, "!r1:hs", "News")
    mx = RecordingSink()
    relay = Relay(mx, FakeFetcher(), BridgeState(), CONTROL, registry=reg,
                  rooms=FakeRoomCreator())

    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "-100222", "Bot", text="story",
                       source_kind="channel", source_label="News")
    )

    assert mx.deliveries[0][0].chat_id == "!r1:hs"


async def test_group_without_room_or_watch_is_still_filtered():
    reg = RoomRegistry()
    reg.register(111, "!dm:hs", "Alice")  # a DM's room says nothing about groups
    mx = RecordingSink()
    relay = Relay(mx, FakeFetcher(), BridgeState(), CONTROL, registry=reg,
                  rooms=FakeRoomCreator())

    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "-100222", "Bot", text="spam",
                       source_kind="channel", source_label="News")
    )

    assert mx.deliveries == []


async def test_unwatch_warns_that_a_room_keeps_relaying():
    reg = RoomRegistry()
    reg.register(-100222, "!r1:hs", "News")
    d, _directory, mx = _delmsg_dispatcher(reg)

    await d.on_matrix_message(_mx(CONTROL, "!tg unwatch news"))

    assert "专属房间" in mx.deliveries[-1][1].text


def _delmsg_dispatcher(reg, dialogs=DIALOGS):
    """A dispatcher plus the directory, for asserting on deletions."""
    directory = FakeDirectory(dialogs)
    mx = RecordingSink()
    accounts = FakeAccounts(directory, FakeSender(), registry=reg)
    d = Dispatcher(
        accounts, mx, BridgeState(), CONTROL,
        command_prefix="!tg", timezone="UTC",
    )
    return d, directory, mx


async def test_delmsg_in_chat_room_asks_to_confirm_first():
    reg = RoomRegistry()
    reg.register(111, "!r1:hs", "Alice")
    d, directory, mx = _delmsg_dispatcher(reg)

    await d.on_matrix_message(_mx("!r1:hs", "!tg delMsg"))

    assert directory.deleted == []  # nothing destroyed without confirmation
    room, text = mx.deliveries[-1][0].chat_id, mx.deliveries[-1][1].text
    assert room == "!r1:hs"  # answered here, not in the control room
    assert "Alice" in text and "!tg delMsg confirm" in text


async def test_delmsg_confirm_in_chat_room_deletes_that_chat():
    reg = RoomRegistry()
    reg.register(111, "!r1:hs", "Alice")
    d, directory, mx = _delmsg_dispatcher(reg)

    await d.on_matrix_message(_mx("!r1:hs", "!tg delMsg confirm"))

    assert directory.deleted == [111]  # this room's chat, no target typed
    assert all(t.chat_id == "!r1:hs" for t, _ in mx.deliveries)


async def test_delmsg_in_chat_room_works_for_unresolvable_chat():
    """The room knows its chat id even when the directory can't name it."""
    reg = RoomRegistry()
    reg.register(777, "!r7:hs", "Ghost")
    d, directory, _ = _delmsg_dispatcher(reg)

    await d.on_matrix_message(_mx("!r7:hs", "!tg delMsg confirm"))

    assert directory.deleted == [777]


async def test_delmsg_in_chat_room_ignores_a_typed_target():
    """In a dedicated room delMsg is about THIS chat; a stray argument that
    happens to name another dialog must not widen the blast radius."""
    reg = RoomRegistry()
    reg.register(111, "!r1:hs", "Alice")
    d, directory, _ = _delmsg_dispatcher(reg)

    await d.on_matrix_message(_mx("!r1:hs", "!tg delMsg AllChat confirm"))

    assert directory.deleted == [111]


# -- control room commands -----------------------------------------------------


async def test_cmd_room_creates_and_registers():
    reg, creator = RoomRegistry(), FakeRoomCreator()
    watched = []
    d, _s, mx = _dispatcher(reg, rooms=creator, on_new_room=watched.append)

    await d.on_matrix_message(_mx(CONTROL, "!tg room alice"))

    assert reg.room_for(111) == "!room1:test"
    assert watched == ["!room1:test"]
    assert "已为" in mx.deliveries[-1][1].text


async def test_cmd_room_is_idempotent():
    reg, creator = RoomRegistry(), FakeRoomCreator()
    reg.register(111, "!existing:hs", "Alice")
    d, _s, mx = _dispatcher(reg, rooms=creator)

    await d.on_matrix_message(_mx(CONTROL, "!tg room alice"))

    assert creator.created == []
    assert "已有专属房间" in mx.deliveries[-1][1].text


async def test_cmd_room_without_space_explains():
    d, _s, mx = _dispatcher(None, rooms=None)
    await d.on_matrix_message(_mx(CONTROL, "!tg room alice"))
    assert "还没绑定空间" in mx.deliveries[-1][1].text


async def test_cmd_rooms_lists_mappings():
    reg = RoomRegistry()
    reg.register(111, "!r1:hs", "Alice")
    reg.register(-100222, "!r2:hs", "News")
    d, _s, mx = _dispatcher(reg)

    await d.on_matrix_message(_mx(CONTROL, "!tg rooms"))

    body = mx.deliveries[-1][1].text
    assert "Alice" in body and "News" in body and "共 2 个" in body


async def test_cmd_rooms_empty_hints_at_creation():
    d, _s, mx = _dispatcher(RoomRegistry())
    await d.on_matrix_message(_mx(CONTROL, "!tg rooms"))
    assert "room" in mx.deliveries[-1][1].text
