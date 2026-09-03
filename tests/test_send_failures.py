"""A send that can never succeed must say so — and mark the room.

"发送失败，见日志" is the right answer for a network blip and the wrong one for
an account that no longer exists: the owner reads it as "try again later" and
keeps typing into a room nothing will ever come out of. Telegram names that
case in the error it returns, so the bridge names it too, and flags the room on
the spot instead of waiting for someone to run `check`.
"""

import pytest

from bridge.core.dispatcher import Dispatcher
from bridge.core.models import Dialog, InboundMessage, MessageKind
from bridge.core.roomregistry import RoomRegistry
from bridge.core.state import BridgeState
from bridge.core.tgerrors import failure_reason, is_permanent

from .fakes import FakeAccounts, FakeDirectory, FakeRoomCreator, RecordingSink

CONTROL = "!control:matrix.org"
ROOM = "!r1:hs"
CHAT = 111
DIALOGS = [Dialog(id=CHAT, name="小明", kind="user", username="qiuqiu")]


# Stand-ins for the Telethon errors, matched by class name exactly as the real
# ones are — so these tests need no Telegram SDK to exercise the real mapping.
class InputUserDeactivatedError(Exception):
    """The other side deleted their Telegram account."""


class PeerIdInvalidError(Exception):
    """The peer cannot be addressed (maybe permanently, maybe a stale hash)."""


class UserDeactivatedError(Exception):
    """*Our* account is the deactivated one."""


class FloodWaitError(Exception):
    """An ordinary, transient refusal."""


# -- classification ------------------------------------------------------------


@pytest.mark.parametrize("exc, expected", [
    (InputUserDeactivatedError(), "deleted"),
    (PeerIdInvalidError(), "gone"),
    (UserDeactivatedError(), "self"),
    (FloodWaitError(), ""),
    (RuntimeError("boom"), ""),
])
def test_failure_reason_names_only_what_telegram_confirmed(exc, expected):
    assert failure_reason(exc) == expected


def test_a_subclass_is_classified_like_its_parent():
    class Wrapped(InputUserDeactivatedError):
        pass

    assert failure_reason(Wrapped()) == "deleted"


def test_only_a_dead_peer_is_permanent():
    # Our own account coming back is a re-login away, so its queue must survive.
    assert is_permanent(InputUserDeactivatedError())
    assert is_permanent(PeerIdInvalidError())
    assert not is_permanent(UserDeactivatedError())
    assert not is_permanent(FloodWaitError())


# -- reporting a failed send ---------------------------------------------------


class ExplodingSender:
    """An OutboundSender that refuses everything with one given error."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.attempts = 0

    async def submit(self, chat_id, dialog_kind, message, at=None,
                     origin_event=None, target_name="", origin_room=None):
        self.attempts += 1
        raise self.exc


def _bridge(exc, presence="ok"):
    """A dispatcher whose one account has a per-chat room and a broken sender."""
    registry = RoomRegistry()
    registry.register(CHAT, ROOM, "小明", kind="user")
    directory = FakeDirectory(DIALOGS)
    directory.presences[CHAT] = presence
    rooms = FakeRoomCreator()
    accounts = FakeAccounts(directory, ExplodingSender(exc),
                            registry=registry, rooms=rooms)
    mx = RecordingSink()
    d = Dispatcher(accounts, mx, BridgeState(), CONTROL,
                   command_prefix="!tg", timezone="UTC")
    return d, mx, registry, rooms


def _typed(text="在吗", room=ROOM):
    return InboundMessage(kind=MessageKind.TEXT, source_room=room,
                          sender="@me", text=text, event_id="$e1")


def _last(mx) -> str:
    return mx.deliveries[-1][1].text


async def test_a_deactivated_account_is_named_and_the_room_marked():
    d, mx, registry, rooms = _bridge(InputUserDeactivatedError())

    await d.on_matrix_message(_typed())

    assert "账户已注销" in _last(mx)
    assert "见日志" not in _last(mx)
    assert rooms.names == [(ROOM, "🗑 小明【已注销】")]
    assert registry.deleted_reason(CHAT) == "deleted"


async def test_the_failure_is_reported_where_it_was_typed():
    d, mx, _, _ = _bridge(InputUserDeactivatedError())

    await d.on_matrix_message(_typed())

    assert mx.deliveries[-1][0].chat_id == ROOM


async def test_the_room_is_not_renamed_again_on_the_next_message():
    d, mx, _, rooms = _bridge(InputUserDeactivatedError())

    await d.on_matrix_message(_typed("在吗"))
    await d.on_matrix_message(_typed("还在吗"))

    assert len(rooms.names) == 1        # the mark is set once...
    assert "账户已注销" in _last(mx)     # ...but every send still explains itself


async def test_an_unexplained_failure_keeps_the_old_answer():
    """A blip must never rename a live room: it says nothing about the peer."""
    d, mx, registry, rooms = _bridge(FloodWaitError())

    await d.on_matrix_message(_typed())

    assert "见日志" in _last(mx)
    assert rooms.names == []
    assert not registry.is_deleted(CHAT)


async def test_our_own_dead_account_is_not_blamed_on_the_other_side():
    d, mx, registry, rooms = _bridge(UserDeactivatedError())

    await d.on_matrix_message(_typed())

    assert "本 Telegram 账户" in _last(mx)
    assert rooms.names == []
    assert not registry.is_deleted(CHAT)


async def test_an_unreachable_peer_is_confirmed_before_the_room_is_renamed():
    """PEER_ID_INVALID also fires on a stale access hash, so it is not proof."""
    d, mx, registry, rooms = _bridge(PeerIdInvalidError(), presence="ok")

    await d.on_matrix_message(_typed())

    assert "见日志" in _last(mx)
    assert rooms.names == []
    assert not registry.is_deleted(CHAT)


async def test_a_confirmed_missing_chat_is_marked_as_deleted_not_deactivated():
    d, mx, registry, rooms = _bridge(PeerIdInvalidError(), presence="gone")

    await d.on_matrix_message(_typed())

    assert "对话已不存在" in _last(mx)
    assert rooms.names == [(ROOM, "🗑 小明（已删除）")]
    assert registry.deleted_reason(CHAT) == "gone"


async def test_the_account_itself_can_upgrade_gone_to_deactivated():
    """A peer first seen as merely unreachable, later confirmed deleted."""
    d, mx, registry, rooms = _bridge(PeerIdInvalidError(), presence="deleted")

    await d.on_matrix_message(_typed())

    assert "账户已注销" in _last(mx)
    assert rooms.names == [(ROOM, "🗑 小明【已注销】")]


async def test_a_chat_with_no_room_still_reports_the_reason():
    """An account with no Space has no rooms to rename — but still a story."""
    registry = RoomRegistry()
    accounts = FakeAccounts(FakeDirectory(DIALOGS),
                            ExplodingSender(InputUserDeactivatedError()),
                            registry=registry, rooms=None)
    mx = RecordingSink()
    d = Dispatcher(accounts, mx, BridgeState(), CONTROL,
                   command_prefix="!tg", timezone="UTC")

    await d.on_matrix_message(
        InboundMessage(kind=MessageKind.TEXT, source_room=CONTROL,
                       sender="@me", text="@qiuqiu 在吗", event_id="$e1")
    )

    assert "账户已注销" in _last(mx)
    assert registry.deleted_reason(CHAT) == "deleted"


# -- a send that fails long after it was queued --------------------------------


async def test_a_deferred_failure_is_reported_to_the_account_it_belongs_to():
    d, mx, registry, rooms = _bridge(FloodWaitError())  # the sender is unused

    await d.on_send_failed(1001, CHAT, "小明", ROOM, InputUserDeactivatedError())

    assert "账户已注销" in _last(mx)
    assert rooms.names == [(ROOM, "🗑 小明【已注销】")]


async def test_a_deferred_failure_for_an_unknown_account_says_nothing():
    d, mx, _, _ = _bridge(FloodWaitError())

    await d.on_send_failed(9999, CHAT, "小明", ROOM, InputUserDeactivatedError())

    assert mx.deliveries == []


class MuteRoomCreator(FakeRoomCreator):
    """Rooms whose rename Matrix refuses (no permission, gone room, …)."""

    async def set_name(self, room_id: str, name: str) -> bool:
        await super().set_name(room_id, name)
        return False


async def test_a_rename_matrix_refused_is_not_claimed_as_done():
    """The mark still stands — but the reply must not describe a room name
    that is not actually there."""
    registry = RoomRegistry()
    registry.register(CHAT, ROOM, "小明", kind="user")
    rooms = MuteRoomCreator()
    accounts = FakeAccounts(FakeDirectory(DIALOGS),
                            ExplodingSender(InputUserDeactivatedError()),
                            registry=registry, rooms=rooms)
    mx = RecordingSink()
    d = Dispatcher(accounts, mx, BridgeState(), CONTROL,
                   command_prefix="!tg", timezone="UTC")

    await d.on_matrix_message(_typed())

    assert "账户已注销" in _last(mx)
    assert "改名" not in _last(mx)
    assert registry.deleted_reason(CHAT) == "deleted"
