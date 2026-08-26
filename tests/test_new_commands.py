"""prefix / join / info commands, and Telegram-side deletion sync."""

import pytest

from bridge.core.dispatcher import Dispatcher
from bridge.core.messagelinks import MessageLinks
from bridge.core.models import ChatInfo, Dialog, InboundMessage, MessageKind
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
DIALOGS = [
    Dialog(id=111, name="Alice", kind="user", username="alice"),
    Dialog(id=-100222, name="News", kind="channel", username="news"),
]


def _dispatcher(state=None, registry=None, rooms=None, directory=None):
    sender, mx = FakeSender(), RecordingSink()
    directory = directory or FakeDirectory(DIALOGS)
    accounts = FakeAccounts(directory, sender, registry=registry, rooms=rooms)
    d = Dispatcher(
        accounts, mx, state or BridgeState(), CONTROL,
        command_prefix="!tg", timezone="UTC",
    )
    return d, sender, mx, directory


def _mx(room, text, event="$e1"):
    return InboundMessage(kind=MessageKind.TEXT, source_room=room, sender="@me",
                          text=text, event_id=event)


def _last(mx):
    return mx.deliveries[-1][1].text


# -- custom prefix -------------------------------------------------------------


async def test_prefix_change_takes_effect():
    state = BridgeState()
    d, _s, mx = _dispatcher(state)[:3]

    await d.on_matrix_message(_mx(CONTROL, "!tg prefix /tg"))
    assert state.command_prefix() == "/tg"

    # Old prefix no longer triggers; new one does.
    await d.on_matrix_message(_mx(CONTROL, "/tg who"))
    assert "当前发送目标" in _last(mx) or "没有发送目标" in _last(mx)


async def test_prefix_rejects_whitespace():
    state = BridgeState()
    d, _s, mx = _dispatcher(state)[:3]
    await d.on_matrix_message(_mx(CONTROL, "!tg prefix a b"))
    assert state.command_prefix() == ""
    assert "空格" in _last(mx)


async def test_prefix_no_arg_shows_current():
    d, _s, mx = _dispatcher()[:3]
    await d.on_matrix_message(_mx(CONTROL, "!tg prefix"))
    assert "当前命令前缀" in _last(mx)


async def test_prefix_persists_in_state(tmp_path):
    path = str(tmp_path / "state.json")
    s1 = BridgeState(path)
    s1.set_command_prefix("!x")
    assert BridgeState(path).command_prefix() == "!x"


# -- join ----------------------------------------------------------------------


async def test_join_reports_success():
    directory = FakeDirectory(DIALOGS)
    directory.join_result = Dialog(id=-100999, name="Joined", kind="group")
    d, _s, mx, _ = _dispatcher(directory=directory)

    await d.on_matrix_message(_mx(CONTROL, "!tg join @somegroup"))

    assert directory.join_calls == ["@somegroup"]
    assert "已加入" in _last(mx) and "Joined" in _last(mx)


async def test_join_failure_is_reported():
    directory = FakeDirectory(DIALOGS)
    directory.join_result = None
    d, _s, mx, _ = _dispatcher(directory=directory)
    await d.on_matrix_message(_mx(CONTROL, "!tg join https://t.me/+deadlink"))
    assert "失败" in _last(mx)


async def test_join_without_arg_is_usage():
    d, _s, mx, _ = _dispatcher()
    await d.on_matrix_message(_mx(CONTROL, "!tg join"))
    assert "用法" in _last(mx)


# -- leave ---------------------------------------------------------------------


async def test_leave_requires_confirmation():
    d, _s, mx, directory = _dispatcher()
    await d.on_matrix_message(_mx(CONTROL, "!tg leave news"))
    assert directory.left == []
    assert "confirm" in _last(mx)


async def test_leave_confirm_leaves_the_channel():
    d, _s, mx, directory = _dispatcher()
    await d.on_matrix_message(_mx(CONTROL, "!tg leave news confirm"))
    assert directory.left == [-100222]
    assert "已退出" in _last(mx)


async def test_leave_says_whether_rejoining_is_possible():
    """A public channel can be rejoined; a private one needs a new invite."""
    d, _s, mx, _ = _dispatcher()
    await d.on_matrix_message(_mx(CONTROL, "!tg leave news"))
    assert "公开的" in _last(mx) and "join @news" in _last(mx)


async def test_leave_warns_when_a_chat_is_private():
    directory = FakeDirectory([Dialog(id=-100777, name="Secret", kind="group")])
    d, _s, mx, _ = _dispatcher(directory=directory)
    await d.on_matrix_message(_mx(CONTROL, "!tg leave Secret"))
    assert "私有的" in _last(mx)


async def test_leave_refuses_a_private_chat():
    """Telegram's "leave" for a DM deletes the whole conversation."""
    d, _s, mx, directory = _dispatcher()
    await d.on_matrix_message(_mx(CONTROL, "!tg leave alice confirm"))
    assert directory.left == []
    assert "私信" in _last(mx)


async def test_leave_unknown_target():
    d, _s, mx, directory = _dispatcher()
    await d.on_matrix_message(_mx(CONTROL, "!tg leave nobody confirm"))
    assert directory.left == [] and "找不到目标" in _last(mx)


async def test_leave_failure_is_reported():
    directory = FakeDirectory(DIALOGS)
    directory.leave_raises = True
    d, _s, mx, _ = _dispatcher(directory=directory)
    await d.on_matrix_message(_mx(CONTROL, "!tg leave news confirm"))
    assert "失败" in _last(mx)


async def test_leave_in_a_chat_room_means_that_chat():
    reg = RoomRegistry()
    reg.register(-100222, "!news:hs", "News")
    d, _s, mx, directory = _dispatcher(registry=reg, rooms=FakeRoomCreator())

    await d.on_matrix_message(_mx("!news:hs", "!tg leave confirm"))

    assert directory.left == [-100222]
    assert mx.deliveries[-1][0].chat_id == "!news:hs"


async def test_leave_keeps_the_dedicated_room():
    """The room is the record of what was said; leaving Telegram isn't a
    reason to destroy it."""
    reg = RoomRegistry()
    reg.register(-100222, "!news:hs", "News")
    d, _s, mx, _ = _dispatcher(registry=reg, rooms=FakeRoomCreator())
    await d.on_matrix_message(_mx(CONTROL, "!tg leave news confirm"))
    assert reg.room_for(-100222) == "!news:hs"
    assert "保留" in _last(mx)


# -- info ----------------------------------------------------------------------


def _info_dir():
    directory = FakeDirectory(DIALOGS)
    directory.infos = {
        "alice": ChatInfo(id=111, kind="user", title="Alice", username="alice",
                          about="hi there", personal_channel="@alicechan"),
        "news": ChatInfo(id=-100222, kind="channel", title="News", username="news",
                         about="daily news", members=4213),
    }
    return directory


async def test_info_renders_user_details():
    d, _s, mx, _ = _dispatcher(directory=_info_dir())
    await d.on_matrix_message(_mx(CONTROL, "!tg info alice"))
    body = _last(mx)
    assert "Alice" in body and "111" in body
    assert "hi there" in body and "@alicechan" in body


async def test_info_renders_channel_members():
    d, _s, mx, _ = _dispatcher(directory=_info_dir())
    await d.on_matrix_message(_mx(CONTROL, "!tg info news"))
    body = _last(mx)
    assert "4213" in body and "daily news" in body


async def test_info_unknown_target():
    d, _s, mx, _ = _dispatcher(directory=_info_dir())
    await d.on_matrix_message(_mx(CONTROL, "!tg info nobody"))
    assert "找不到目标" in _last(mx)


async def test_info_in_chat_room_uses_that_chat_and_refreshes_topic():
    reg = RoomRegistry()
    reg.register(111, "!r1:hs", "Alice")
    rooms = FakeRoomCreator()
    directory = _info_dir()
    directory.infos["111"] = directory.infos["alice"]  # looked up by id in-room
    d, _s, mx, _ = _dispatcher(registry=reg, rooms=rooms, directory=directory)

    # No target given, typed inside the per-chat room -> resolves to chat 111.
    await d.on_matrix_message(_mx("!r1:hs", "!tg info"))

    assert directory.info_calls == ["111"]
    assert mx.deliveries[-1][0].chat_id == "!r1:hs"   # reply lands in the room
    assert rooms.topics and rooms.topics[-1][0] == "!r1:hs"  # topic refreshed


async def test_info_lookup_by_id_in_room(monkeypatch):
    reg = RoomRegistry()
    reg.register(111, "!r1:hs", "Alice")
    directory = _info_dir()
    directory.infos["111"] = directory.infos["alice"]
    d, _s, mx, _ = _dispatcher(registry=reg, rooms=FakeRoomCreator(),
                               directory=directory)
    await d.on_matrix_message(_mx("!r1:hs", "!tg info"))
    assert "Alice" in _last(mx)


async def test_other_command_in_chat_room_still_intercepted():
    reg = RoomRegistry()
    reg.register(111, "!r1:hs", "Alice")
    d, sender, mx, _ = _dispatcher(registry=reg, rooms=FakeRoomCreator())
    await d.on_matrix_message(_mx("!r1:hs", "!tg mute alice"))
    assert sender.submissions == []
    assert "控制房间" in _last(mx)


# -- deletion sync (relay side) ------------------------------------------------


def _relay(links, editor):
    return Relay(
        matrix_sink=RecordingSink(return_id="$ev"), telegram_fetcher=FakeFetcher(),
        state=BridgeState(), control_room=CONTROL,
        links=links, editor=editor,
    )


class _Editor:
    """Records the marks made on Matrix events (never destroys anything)."""

    def __init__(self):
        self.replaced = []   # (room, event, html)
        self.annotated = []  # (room, event, html)

    async def replace_event(self, room_id, event_id, html):
        self.replaced.append((room_id, event_id, html))

    async def annotate_event(self, room_id, event_id, html):
        self.annotated.append((room_id, event_id, html))


async def test_remote_deletion_marks_instead_of_deleting():
    """The Matrix copy must survive: struck through, not redacted."""
    links = MessageLinks(clock=lambda: 1.0)
    links.add(111, 42, "user", "!dm:hs", "$dm", "secret plan")
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_deleted(None, [42])  # DM delete: no chat id

    room, event, html = ed.replaced[0]
    assert (room, event) == ("!dm:hs", "$dm")
    assert "secret plan" in html and "<del>" in html and "已被删除" in html
    assert links.find(111, 42) is None  # terminal: repeat updates are no-ops


async def test_media_deletion_annotates_rather_than_replacing():
    """Replacing a media event with text would destroy the file."""
    links = MessageLinks(clock=lambda: 1.0)
    links.add(111, 43, "user", "!dm:hs", "$img", "a caption", media=True)
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_deleted(None, [43])

    assert ed.replaced == []
    assert ed.annotated[0][:2] == ("!dm:hs", "$img")


async def test_deletion_keeps_the_sender_prefix():
    links = MessageLinks(clock=lambda: 1.0)
    links.add(-100222, 7, "channel", "!chan:hs", "$c", "post", head="<b>News</b>")
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_deleted(-100222, [7])

    html = ed.replaced[0][2]
    assert html.startswith("<b>News</b>: ") and "<del>post</del>" in html


async def test_channel_deletion_uses_chat_scoped_lookup():
    links = MessageLinks(clock=lambda: 1.0)
    links.add(-100222, 7, "channel", "!chan:hs", "$c", "post")
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_deleted(-100222, [7])

    assert ed.replaced[0][:2] == ("!chan:hs", "$c")


async def test_delete_arriving_before_the_relay_is_applied_on_arrival():
    """Spam is often deleted in the same second it is posted.

    The delete update then overtakes the relay and finds no link yet. Dropping
    it is what made deletion sync look flaky — the message stayed in Matrix
    with no mark, for good.
    """
    links = MessageLinks(clock=lambda: 1.0)
    ed = _Editor()
    state = BridgeState()
    state.watch("-100222")
    relay = Relay(
        matrix_sink=RecordingSink(return_id="$ev"), telegram_fetcher=FakeFetcher(),
        state=state, control_room=CONTROL, links=links, editor=ed,
    )

    # The delete lands first...
    await relay.on_telegram_deleted(-100222, [99])
    assert ed.replaced == []

    # ...then the message it refers to finally gets relayed.
    await relay.on_telegram_message(InboundMessage(
        MessageKind.TEXT, "-100222", "Spammer", text="join my channel",
        source_kind="group", source_label="Group", source_msg_id=99,
    ))

    room, event, html = ed.replaced[0]
    assert event == "$ev"
    assert "join my channel" in html and "已被删除" in html


async def test_pending_delete_matches_a_chatless_update():
    """DM deletions omit the chat id, so the tombstone has to match on msg id."""
    links = MessageLinks(clock=lambda: 1.0)
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_deleted(None, [42])
    await relay.on_telegram_message(InboundMessage(
        MessageKind.TEXT, "111", "Alice", text="hi",
        source_kind="user", source_msg_id=42,
    ))

    assert ed.replaced and "hi" in ed.replaced[0][2]


async def test_pending_delete_does_not_mark_an_unrelated_message():
    links = MessageLinks(clock=lambda: 1.0)
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_deleted(None, [42])
    await relay.on_telegram_message(InboundMessage(
        MessageKind.TEXT, "111", "Alice", text="a different message",
        source_kind="user", source_msg_id=43,
    ))

    assert ed.replaced == []


async def test_pending_delete_is_consumed_once():
    """A later message reusing that id in another chat must not be marked."""
    links = MessageLinks(clock=lambda: 1.0)
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_deleted(None, [42])
    for chat in ("111", "222"):
        await relay.on_telegram_message(InboundMessage(
            MessageKind.TEXT, chat, "Alice", text="hi",
            source_kind="user", source_msg_id=42,
        ))

    assert len(ed.replaced) == 1


async def test_pending_deletes_expire():
    """A tombstone whose message never arrives must not linger for ever."""
    now = [1000.0]
    links = MessageLinks(clock=lambda: 1.0)
    ed = _Editor()
    relay = Relay(
        matrix_sink=RecordingSink(return_id="$ev"), telegram_fetcher=FakeFetcher(),
        state=BridgeState(), control_room=CONTROL, links=links, editor=ed,
        pending_delete_ttl=60.0, clock=lambda: now[0],
    )

    await relay.on_telegram_deleted(None, [42])
    now[0] += 3600
    await relay.on_telegram_message(InboundMessage(
        MessageKind.TEXT, "111", "Alice", text="much later",
        source_kind="user", source_msg_id=42,
    ))

    assert ed.replaced == []


async def test_deletes_from_unrelayed_chats_are_not_remembered():
    """Every group the account is in deletes messages constantly."""
    relay = _relay(MessageLinks(clock=lambda: 1.0), _Editor())

    await relay.on_telegram_deleted(-100999, [1])  # not watched, no room

    assert relay._pending_deletes == {}


async def test_unknown_deletion_is_ignored():
    ed = _Editor()
    relay = _relay(MessageLinks(clock=lambda: 1.0), ed)
    await relay.on_telegram_deleted(None, [999])
    assert ed.replaced == [] and ed.annotated == []


# -- edit sync -----------------------------------------------------------------


async def test_edit_replaces_in_place_showing_old_and_new():
    """Edited in place (m.replace) AND both versions visible in the body."""
    links = MessageLinks(clock=lambda: 1.0)
    links.add(111, 42, "user", "!dm:hs", "$dm", "before")
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_edited(111, 42, "after")

    room, event, html = ed.replaced[0]
    assert (room, event) == ("!dm:hs", "$dm")
    assert "after" in html and "before" in html
    assert "<del>before</del>" in html   # original struck through
    assert ed.annotated == []            # no extra event cluttering the room
    assert links.find(111, 42).text == "after"


async def test_edit_preserves_the_original_sender_prefix():
    """A group message must not lose its '<b>sender</b>: ' head when edited."""
    links = MessageLinks(clock=lambda: 1.0)
    links.add(-100222, 7, "channel", "!chan:hs", "$c", "v1",
              head="<b>News</b>")
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_edited(-100222, 7, "v2")

    assert ed.replaced[0][2].startswith("<b>News</b>: v2")


async def test_repeated_edits_keep_showing_the_first_original():
    """After three edits you still see what the message originally said."""
    links = MessageLinks(clock=lambda: 1.0)
    links.add(111, 42, "user", "!dm:hs", "$dm", "v1")
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_edited(111, 42, "v2")
    await relay.on_telegram_edited(111, 42, "v3")

    last = ed.replaced[-1][2]
    assert "v3" in last and "<del>v1</del>" in last
    assert "v2" not in last


async def test_edit_escapes_html_in_the_new_text():
    links = MessageLinks(clock=lambda: 1.0)
    links.add(111, 42, "user", "!dm:hs", "$dm", "before")
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_edited(111, 42, "<script>x</script>")

    assert "<script>" not in ed.replaced[0][2]
    assert "&lt;script&gt;" in ed.replaced[0][2]


async def test_media_edit_annotates_rather_than_replacing():
    """Replacing a media event with text would drop the file."""
    links = MessageLinks(clock=lambda: 1.0)
    links.add(111, 43, "user", "!dm:hs", "$img", "old cap", media=True)
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_edited(111, 43, "new cap")

    assert ed.replaced == []
    assert "new cap" in ed.annotated[0][2] and "old cap" in ed.annotated[0][2]


async def test_successive_edits_always_target_the_same_event():
    links = MessageLinks(clock=lambda: 1.0)
    links.add(111, 42, "user", "!dm:hs", "$dm", "v1")
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_edited(111, 42, "v2")
    await relay.on_telegram_edited(111, 42, "v3")

    assert all(r[1] == "$dm" for r in ed.replaced)  # never a new event


async def test_edit_found_despite_chat_id_mismatch():
    """A DM/basic-group id-form mismatch must not silently drop the edit."""
    links = MessageLinks(clock=lambda: 1.0)
    links.add(111, 42, "user", "!dm:hs", "$dm", "before")
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_edited(999, 42, "after")  # wrong chat id

    assert ed.replaced and ed.replaced[0][1] == "$dm"


async def test_channel_edit_never_falls_back_across_chats():
    """Channel msg ids repeat per channel — a fallback would cross the wires."""
    links = MessageLinks(clock=lambda: 1.0)
    links.add(-100111, 7, "channel", "!a:hs", "$a", "one")
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_edited(-100222, 7, "two")  # a different channel

    assert ed.replaced == [] and ed.annotated == []


async def test_unchanged_text_is_not_announced():
    """Telegram 'edits' a message when it attaches a link preview - ignore it."""
    links = MessageLinks(clock=lambda: 1.0)
    links.add(111, 42, "user", "!dm:hs", "$dm", "same")
    ed = _Editor()
    relay = _relay(links, ed)

    await relay.on_telegram_edited(111, 42, "same")

    assert ed.annotated == []


async def test_edit_of_unknown_message_is_ignored():
    ed = _Editor()
    relay = _relay(MessageLinks(clock=lambda: 1.0), ed)
    await relay.on_telegram_edited(111, 999, "hi")
    assert ed.annotated == []


async def test_relay_records_link_and_text_on_incoming():
    links = MessageLinks(clock=lambda: 1.0)
    relay = Relay(
        matrix_sink=RecordingSink(return_id="$ev9"), telegram_fetcher=FakeFetcher(),
        state=BridgeState(), control_room=CONTROL, links=links,
    )
    msg = InboundMessage(
        kind=MessageKind.TEXT, source_room="111", sender="Alice", text="hi",
        source_label="Alice", source_kind="user", source_msg_id=55,
    )
    await relay.on_telegram_message(msg)
    link = links.find(111, 55)
    assert link.event_id == "$ev9"
    assert link.text == "hi"  # needed later for edit diffs / delete marking


async def test_info_id_line_carries_username_and_id():
    d, _s, mx, _ = _dispatcher(directory=_info_dir())
    await d.on_matrix_message(_mx(CONTROL, "!tg info alice"))
    body = _last(mx)
    assert "@alice · 111" in body
