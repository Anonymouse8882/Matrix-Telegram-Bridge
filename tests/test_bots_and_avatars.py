"""Bots as their own kind, and Telegram -> Matrix avatar mirroring.

A bot DM is not correspondence: service and spam bots message you unprompted,
and under the old "every DM relays" rule each earned a room nobody asked for.
Bots therefore relay only from the allow-list, while a real person's DM still
relays by default.
"""

from types import SimpleNamespace

from bridge.adapters.telegram_user_source import _dialog_of, _entity_to_dialog
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
BOT_CHAT = "900000001"


# -- classifying a bot ---------------------------------------------------------


def _dialog(is_user=True, is_group=False, bot=False, name="X", did=1):
    entity = SimpleNamespace(username=None, bot=bot)
    return SimpleNamespace(
        id=did, name=name, entity=entity, is_user=is_user, is_group=is_group
    )


def test_a_bot_dialog_is_its_own_kind():
    assert _dialog_of(_dialog(bot=True)).kind == "bot"


def test_a_person_is_still_a_user():
    assert _dialog_of(_dialog(bot=False)).kind == "user"


def test_a_group_is_unaffected_by_the_bot_split():
    assert _dialog_of(_dialog(is_user=False, is_group=True)).kind == "group"


def test_entity_lookup_also_reports_bots():
    from telethon.tl.types import User

    bot = User(id=7, bot=True)
    person = User(id=8, bot=False)
    assert _entity_to_dialog(bot).kind == "bot"
    assert _entity_to_dialog(person).kind == "user"


# -- relay filter --------------------------------------------------------------


def _relay(watched=(), registry=None, rooms=None):
    mx = RecordingSink(return_id="$e")
    state = BridgeState()
    for w in watched:
        state.watch(w)
    relay = Relay(mx, FakeFetcher(), state, CONTROL, registry=registry, rooms=rooms)
    return relay, mx


def _bot_msg(text="/start reply"):
    return InboundMessage(
        MessageKind.TEXT, BOT_CHAT, "Spam Info Bot", text=text,
        source_kind="bot", source_label="Spam Info Bot",
    )


async def test_a_bot_dm_is_not_relayed_by_default():
    """The whole point: service/spam bots stop earning rooms unasked."""
    relay, mx = _relay()
    await relay.on_telegram_message(_bot_msg())
    assert mx.deliveries == []


async def test_a_watched_bot_is_relayed():
    relay, mx = _relay(watched=[BOT_CHAT])
    await relay.on_telegram_message(_bot_msg("your code is 12345"))
    assert "your code is 12345" in mx.deliveries[0][1].text


async def test_a_bot_room_alone_does_not_relay():
    """Unlike a group, an existing room is NOT opt-in for a bot — otherwise the
    rooms already created for spam bots would keep filling up."""
    reg = RoomRegistry()
    reg.register(int(BOT_CHAT), "!spam:hs", "Spam Info Bot", kind="bot")
    relay, mx = _relay(registry=reg, rooms=FakeRoomCreator())

    await relay.on_telegram_message(_bot_msg())

    assert mx.deliveries == []


async def test_a_person_dm_still_relays_without_any_opt_in():
    relay, mx = _relay()
    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "111", "Alice", text="hi",
                       source_kind="user", source_label="Alice")
    )
    assert "hi" in mx.deliveries[0][1].text


async def test_a_watched_bots_own_room_drops_the_redundant_prefix():
    """A bot room is one-to-one, so the sender line is noise — same as a DM."""
    reg = RoomRegistry()
    reg.register(int(BOT_CHAT), "!spam:hs", "Spam Info Bot", kind="bot")
    relay, mx = _relay(watched=[BOT_CHAT], registry=reg, rooms=FakeRoomCreator())

    await relay.on_telegram_message(_bot_msg("code 42"))

    assert mx.deliveries[0][1].text == "code 42"


# -- dispatcher: watching a bot ------------------------------------------------

DIALOGS = [
    Dialog(id=111, name="Alice", kind="user", username="alice"),
    Dialog(id=900000001, name="Spam Info Bot", kind="bot", username="spambot"),
    Dialog(id=-100222, name="News", kind="channel", username="news"),
]


def _dispatcher(registry=None, rooms=None, state=None):
    directory = FakeDirectory(DIALOGS)
    mx = RecordingSink()
    accounts = FakeAccounts(directory, FakeSender(), registry=registry,
                            rooms=rooms, state=state)
    d = Dispatcher(accounts, mx, BridgeState(), CONTROL,
                   command_prefix="!tg", timezone="UTC")
    return d, mx, accounts


def _mx(text, room=CONTROL):
    return InboundMessage(kind=MessageKind.TEXT, source_room=room,
                          sender="@me", text=text, event_id="$e1")


async def test_watch_accepts_a_bot():
    state = BridgeState()
    d, mx, _ = _dispatcher(state=state)
    await d.on_matrix_message(_mx("!tg watch spambot"))
    assert state.is_watched("900000001")
    assert "白名单" in mx.deliveries[-1][1].text


async def test_watch_still_refuses_a_person():
    state = BridgeState()
    d, mx, _ = _dispatcher(state=state)
    await d.on_matrix_message(_mx("!tg watch alice"))
    assert not state.is_watched("111")
    assert "默认就转发" in mx.deliveries[-1][1].text


async def test_unwatching_a_bot_says_its_room_goes_quiet():
    state = BridgeState()
    state.watch("900000001")
    reg = RoomRegistry()
    reg.register(900000001, "!spam:hs", "Spam Info Bot", kind="bot")
    d, mx, _ = _dispatcher(registry=reg, state=state)

    await d.on_matrix_message(_mx("!tg unwatch spambot"))

    assert not state.is_watched("900000001")
    assert "不再转发" in mx.deliveries[-1][1].text


async def test_making_a_room_for_a_bot_also_watches_it():
    """Asking for the room is asking for its messages; without the watch the
    new room would sit empty for ever."""
    state = BridgeState()
    reg, creator = RoomRegistry(), FakeRoomCreator()
    d, mx, _ = _dispatcher(registry=reg, rooms=creator, state=state)

    await d.on_matrix_message(_mx("!tg room spambot"))

    assert reg.room_for(900000001) == "!room1:test"
    assert state.is_watched("900000001")
    assert "白名单" in mx.deliveries[-1][1].text


async def test_list_marks_a_watched_bot_as_relayed():
    state = BridgeState()
    state.watch("900000001")
    d, mx, _ = _dispatcher(state=state)

    await d.on_matrix_message(_mx("!tg list"))

    body = mx.deliveries[-1][1].text
    assert "机器人" in body
    assert "Spam Info Bot" in body and "👁" in body


async def test_list_does_not_mark_an_unwatched_bot_with_a_room():
    reg = RoomRegistry()
    reg.register(900000001, "!spam:hs", "Spam Info Bot", kind="bot")
    d, mx, _ = _dispatcher(registry=reg)

    await d.on_matrix_message(_mx("!tg list"))

    line = [l for l in mx.deliveries[-1][1].text.split("\n")
            if "Spam Info Bot" in l][0]
    assert "👁" not in line  # a room is not opt-in for a bot


# -- fetching a user's photo ---------------------------------------------------


class _AvatarClient:
    """A Telethon stand-in for the two-step avatar fetch."""

    def __init__(self, short=None, full_photo=None, short_raises=False):
        self.short = short              # download_profile_photo result
        self.full_photo = full_photo    # full_user.profile_photo
        self.short_raises = short_raises
        self.full_calls = 0
        self.downloaded: list = []

    async def download_profile_photo(self, entity, file=None):
        if self.short_raises:
            raise RuntimeError("entity not found (test)")
        return self.short

    async def __call__(self, request):
        self.full_calls += 1
        return SimpleNamespace(
            full_user=SimpleNamespace(
                profile_photo=self.full_photo,
                personal_photo=None,
                fallback_photo=None,
            )
        )

    async def download_media(self, photo, file=None):
        self.downloaded.append(photo)
        return b"FULLPHOTO"


def _source(client):
    from bridge.adapters.telegram_user_source import TelegramUserSource

    return TelegramUserSource(client)


async def test_a_users_photo_comes_from_the_short_entity_when_present():
    c = _AvatarClient(short=b"SHORTPHOTO")
    assert await _source(c).avatar(5550001234) == b"SHORTPHOTO"
    assert c.full_calls == 0  # no need for the expensive lookup


async def test_a_min_users_photo_is_recovered_from_the_full_record():
    """`entity.photo` is empty for a user we only know from a message, even
    when the account can see the photo — which is why a DM's room could end up
    with no avatar while groups all got one."""
    c = _AvatarClient(short=None, full_photo=SimpleNamespace(id=9))
    assert await _source(c).avatar(5550001234) == b"FULLPHOTO"
    assert c.full_calls == 1


async def test_a_user_with_genuinely_no_photo_gives_none():
    c = _AvatarClient(short=None, full_photo=None)
    assert await _source(c).avatar(5550001234) is None


async def test_a_group_does_not_get_the_full_user_fallback():
    c = _AvatarClient(short=None)
    assert await _source(c).avatar(-1009876543210) is None
    assert c.full_calls == 0  # GetFullUser on a group id would be nonsense


async def test_a_failed_short_fetch_still_tries_the_full_record():
    c = _AvatarClient(short_raises=True, full_photo=SimpleNamespace(id=9))
    assert await _source(c).avatar(5550001234) is None  # errors stop there
    assert c.full_calls == 0


# -- avatars -------------------------------------------------------------------


async def test_a_new_room_gets_the_chat_avatar():
    reg, creator = RoomRegistry(), FakeRoomCreator()
    mx = RecordingSink(return_id="$e")
    relay = Relay(mx, FakeFetcher(), BridgeState(), CONTROL,
                  registry=reg, rooms=creator)

    await relay.on_telegram_message(
        InboundMessage(MessageKind.TEXT, "111", "Alice", text="hi",
                       source_kind="user", source_label="Alice")
    )

    assert creator.avatars == [("!room1:test", 111)]


async def test_avatar_command_syncs_one_room():
    reg, creator = RoomRegistry(), FakeRoomCreator()
    reg.register(111, "!r1:hs", "Alice", kind="user")
    d, mx, _ = _dispatcher(registry=reg, rooms=creator)

    await d.on_matrix_message(_mx("!tg avatar alice"))

    assert creator.avatars == [("!r1:hs", 111)]
    assert "已同步" in mx.deliveries[-1][1].text


async def test_avatar_command_says_the_chat_has_no_photo():
    """"没有更新" hid two very different causes; the reason is the useful bit."""
    reg, creator = RoomRegistry(), FakeRoomCreator()
    creator.avatar_result = "none"
    reg.register(111, "!r1:hs", "Alice", kind="user")
    d, mx, _ = _dispatcher(registry=reg, rooms=creator)

    await d.on_matrix_message(_mx("!tg avatar alice"))

    assert "没有可获取的头像" in mx.deliveries[-1][1].text


async def test_avatar_command_distinguishes_a_failure():
    reg, creator = RoomRegistry(), FakeRoomCreator()
    creator.avatar_result = "error"
    reg.register(111, "!r1:hs", "Alice", kind="user")
    d, mx, _ = _dispatcher(registry=reg, rooms=creator)

    await d.on_matrix_message(_mx("!tg avatar alice"))

    assert "失败" in mx.deliveries[-1][1].text


async def test_avatar_all_syncs_every_mapped_room():
    reg, creator = RoomRegistry(), FakeRoomCreator()
    reg.register(111, "!r1:hs", "Alice", kind="user")
    reg.register(-100222, "!r2:hs", "News", kind="channel")
    d, mx, _ = _dispatcher(registry=reg, rooms=creator)

    await d.on_matrix_message(_mx("!tg avatar all"))

    assert sorted(creator.avatars) == [("!r1:hs", 111), ("!r2:hs", -100222)]
    assert "已更新 2 个" in mx.deliveries[-1][1].text


async def test_avatar_all_names_the_rooms_it_could_not_do():
    """"4/6 更新" left you guessing which two were missing, and why."""
    reg, creator = RoomRegistry(), FakeRoomCreator()
    creator.avatar_result = "none"
    reg.register(5550001234, "!r1:hs", "小明", kind="user")
    d, mx, _ = _dispatcher(registry=reg, rooms=creator)

    await d.on_matrix_message(_mx("!tg avatar all"))

    body = mx.deliveries[-1][1].text
    assert "小明" in body and "没有头像" in body


async def test_avatar_in_a_chat_room_needs_no_target():
    reg, creator = RoomRegistry(), FakeRoomCreator()
    reg.register(111, "!r1:hs", "Alice", kind="user")
    d, mx, _ = _dispatcher(registry=reg, rooms=creator)

    await d.on_matrix_message(_mx("!tg avatar", room="!r1:hs"))

    assert creator.avatars == [("!r1:hs", 111)]


async def test_avatar_without_a_room_explains_instead_of_failing():
    reg, creator = RoomRegistry(), FakeRoomCreator()
    d, mx, _ = _dispatcher(registry=reg, rooms=creator)

    await d.on_matrix_message(_mx("!tg avatar alice"))

    assert creator.avatars == []
    assert "还没有专属房间" in mx.deliveries[-1][1].text
