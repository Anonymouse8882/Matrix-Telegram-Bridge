"""Dispatcher tests: commands + dynamic outgoing routing, all via fakes."""

from datetime import datetime, timezone

import pytest

from bridge.core.dispatcher import Dispatcher
from bridge.core.models import Dialog, InboundMessage, MediaRef, MessageKind
from bridge.core.replymap import ReplyMap, ReplyRef
from bridge.core.state import BridgeState

from .fakes import FakeAccounts, FakeDirectory, FakeSender, RecordingSink

ROOM = "!room:matrix.org"
# Settings and the active target live in the account's own state file now.
TARGET_KEY = "@current"   # the slot inside the account's own state

DIALOGS = [
    Dialog(id=111, name="Alice", kind="user", username="alice"),
    Dialog(id=-100222, name="News Channel", kind="channel", username="news"),
    Dialog(id=-333, name="My Group", kind="group"),
]


def _build(active=None, muted=(), watched=(), reply_map=None, links=None):
    sender = FakeSender()          # outgoing Telegram (records submissions)
    mx = RecordingSink()           # replies posted back to Matrix
    directory = FakeDirectory(DIALOGS)
    state = BridgeState()
    accounts = FakeAccounts(directory, sender, reply_map=reply_map, state=state,
                            links=links)
    if active:
        state.set_active_target(TARGET_KEY, active)
    for m in muted:
        state.mute(m)
    for w in watched:
        state.watch(w)
    d = Dispatcher(accounts, mx, state, ROOM,
                   command_prefix="!tg", timezone="UTC")
    return d, sender, mx, state, directory


def _text(body):
    return InboundMessage(MessageKind.TEXT, ROOM, "@bot", text=body)


async def test_message_ignored_from_other_room():
    d, sender, mx, _, _ = _build(active="111")
    await d.on_matrix_message(
        InboundMessage(MessageKind.TEXT, "!elsewhere", "@bot", text="hi")
    )
    assert sender.submissions == []


async def test_no_active_target_prompts_user():
    d, sender, mx, _, _ = _build(active=None)
    await d.on_matrix_message(_text("hello"))
    assert sender.submissions == []
    assert len(mx.deliveries) == 1
    assert "use" in mx.deliveries[0][1].text.lower()


async def test_send_to_active_target():
    d, sender, mx, _, _ = _build(active="111")
    await d.on_matrix_message(_text("hello alice"))
    chat_id, kind, message, at = sender.submissions[0]
    assert chat_id == 111 and kind == "user"
    assert message.text == "hello alice"
    assert at is None


async def test_at_prefix_overrides_target_once():
    d, sender, mx, state, _ = _build(active="111")
    await d.on_matrix_message(_text("@news hot story"))
    chat_id, kind, message, _ = sender.submissions[0]
    assert chat_id == -100222 and kind == "channel"
    assert message.text == "hot story"
    assert state.active_target(TARGET_KEY) == "111"   # unchanged


async def test_unknown_target_reports_back():
    d, sender, mx, _, _ = _build(active="111")
    await d.on_matrix_message(_text("@nobody hi"))
    assert sender.submissions == []
    assert "找不到" in mx.deliveries[0][1].text


async def test_cmd_use_sets_active_target():
    d, sender, mx, state, _ = _build()
    await d.on_matrix_message(_text("!tg use news"))
    assert state.active_target(TARGET_KEY) == "-100222"


async def test_cmd_list_grouped_with_marks():
    d, sender, mx, _, _ = _build(active="111", muted=["-333"], watched=["-100222"])
    await d.on_matrix_message(_text("!tg list"))
    body = mx.deliveries[0][1].text
    assert "私信" in body and "群组" in body and "频道" in body
    assert "Alice" in body and "News Channel" in body
    assert "⭐" in body and "👁" in body and "🔕" in body


async def test_cmd_watch_and_unwatch():
    d, sender, mx, state, _ = _build()
    await d.on_matrix_message(_text("!tg watch news"))
    assert state.is_watched("-100222")
    await d.on_matrix_message(_text("!tg unwatch news"))
    assert not state.is_watched("-100222")


async def test_cmd_watch_on_dm_is_noop():
    d, sender, mx, state, _ = _build()
    await d.on_matrix_message(_text("!tg watch alice"))
    assert not state.is_watched("111")
    assert "私信" in mx.deliveries[0][1].text


async def test_media_submitted_with_ref_not_bytes():
    d, sender, mx, _, _ = _build(active="-333")
    ref = MediaRef(uri="mxc://s/1", mimetype="image/png", filename="p.png")
    await d.on_matrix_message(
        InboundMessage(MessageKind.IMAGE, ROOM, "@bot", media=ref)
    )
    chat_id, kind, message, _ = sender.submissions[0]
    assert chat_id == -333
    assert message.kind is MessageKind.IMAGE
    assert message.media is ref          # scheduler fetches bytes later
    assert message.media_bytes is None


async def test_cmd_at_schedules_send():
    d, sender, mx, _, _ = _build(active="111")
    await d.on_matrix_message(_text("!tg at 2030-01-02 03:04 hello later"))
    chat_id, kind, message, at = sender.submissions[0]
    expected = datetime(2030, 1, 2, 3, 4, tzinfo=timezone.utc).timestamp()
    assert chat_id == 111
    assert message.text == "hello later"
    assert at == expected
    assert "已排期" in mx.deliveries[-1][1].text


async def test_cmd_at_bad_format():
    d, sender, mx, _, _ = _build(active="111")
    await d.on_matrix_message(_text("!tg at not-a-date nope"))
    assert sender.submissions == []
    assert "用法" in mx.deliveries[0][1].text


async def test_cmd_delay_set_and_view():
    d, sender, mx, state, _ = _build()
    await d.on_matrix_message(_text("!tg delay 5s 30s"))
    assert state.delay_fixed() == 5
    assert state.delay_random() == 30
    await d.on_matrix_message(_text("!tg delay"))
    assert "固定" in mx.deliveries[-1][1].text


async def test_cmd_selfdestruct_set_and_view():
    d, sender, mx, state, _ = _build()
    await d.on_matrix_message(_text("!tg selfdestruct 群组 1h30m"))
    assert state.self_destruct("group") == 5400
    await d.on_matrix_message(_text("!tg selfdestruct"))
    assert "1小时30分" in mx.deliveries[-1][1].text


async def test_delmsg_requires_confirmation():
    d, sender, mx, state, directory = _build()
    await d.on_matrix_message(_text("!tg delMsg news"))
    assert directory.deleted == []
    assert "confirm" in mx.deliveries[-1][1].text


async def test_delmsg_single_with_confirm():
    d, sender, mx, state, directory = _build()
    await d.on_matrix_message(_text("!tg delMsg news confirm"))
    assert directory.deleted == [-100222]
    assert "已删除" in mx.deliveries[-1][1].text


async def test_delmsg_allchat_covers_everything():
    d, sender, mx, state, directory = _build()
    await d.on_matrix_message(_text("!tg delMsg AllChat confirm"))
    assert set(directory.deleted) == {111, -100222, -333}


async def test_reply_routes_to_source_chat_as_reply():
    rm = ReplyMap()
    rm.remember("$evt1", ReplyRef(chat_id=-100222, msg_id=42, kind="channel", name="News"))
    d, sender, mx, _, _ = _build(active="111", reply_map=rm)
    await d.on_matrix_message(
        InboundMessage(MessageKind.TEXT, ROOM, "@bot", text="my reply",
                       reply_to_event="$evt1")
    )
    chat_id, kind, message, _ = sender.submissions[0]
    assert chat_id == -100222 and kind == "channel"  # not the active target 111
    assert message.text == "my reply"
    assert message.reply_to == 42


async def test_reply_survives_a_restart_via_the_persisted_links():
    """A restart empties the ReplyMap; the link file still knows the chat."""
    from bridge.core.messagelinks import MessageLinks

    links = MessageLinks(clock=lambda: 1.0)
    links.add(-100222, 42, "channel", ROOM, "$evt1", "the relayed text")
    d, sender, mx, _, _ = _build(active="111", reply_map=ReplyMap(), links=links)
    await d.on_matrix_message(
        InboundMessage(MessageKind.TEXT, ROOM, "@bot", text="my reply",
                       reply_to_event="$evt1")
    )
    chat_id, kind, message, _ = sender.submissions[0]
    assert chat_id == -100222 and kind == "channel"   # not the active target
    assert message.reply_to == 42


async def test_reply_to_unknown_event_is_refused_not_redirected():
    """Falling through would deliver a private reply to whoever happens to be
    the active target — and Element strips the quote, so nothing would show
    that it went to the wrong person."""
    d, sender, mx, _, _ = _build(active="111", reply_map=ReplyMap())
    await d.on_matrix_message(
        InboundMessage(MessageKind.TEXT, ROOM, "@bot", text="hi",
                       reply_to_event="$missing")
    )
    assert sender.submissions == []      # nothing sent to anyone
    assert "没有发送" in mx.deliveries[-1][1].text


async def test_outgoing_passes_origin_event():
    d, sender, mx, _, _ = _build(active="111")
    await d.on_matrix_message(
        InboundMessage(MessageKind.TEXT, ROOM, "@bot", text="hi", event_id="$myevt")
    )
    assert sender.origins[0] == "$myevt"     # so it can be mapped for reply/delete
    assert sender.names[0] == "Alice"


async def test_on_redaction_deletes_mapped_tg_message():
    rm = ReplyMap()
    rm.remember("$e", ReplyRef(chat_id=-100222, msg_id=42, kind="channel", name="News"))
    d, sender, mx, _, directory = _build(reply_map=rm)
    await d.on_redaction("$e")
    assert directory.deleted_single == [(-100222, 42)]


async def test_on_redaction_ignores_unmapped_event():
    rm = ReplyMap()
    d, sender, mx, _, directory = _build(reply_map=rm)
    await d.on_redaction("$unknown")
    assert directory.deleted_single == []     # never touch unrelated messages


async def test_on_redaction_reports_permission_failure():
    rm = ReplyMap()
    rm.remember("$e", ReplyRef(chat_id=-100222, msg_id=42, kind="channel", name="News"))
    d, sender, mx, _, directory = _build(reply_map=rm)
    directory.delete_raises = True
    await d.on_redaction("$e")
    assert "无法删除" in mx.deliveries[-1][1].text


async def test_cmd_settings_summarizes_state():
    d, sender, mx, state, _ = _build(active="111", muted=["-333"], watched=["-100222"])
    state.set_delay(5, 30)
    state.set_self_destruct("user", 3600)
    await d.on_matrix_message(_text("!tg settings"))
    body = mx.deliveries[-1][1].text
    assert "当前设置" in body
    assert "Alice" in body            # active target resolved
    assert "固定" in body             # delay shown
    assert "1小时" in body            # self-destruct shown
    assert "Asia" in body or "UTC" in body  # timezone shown


async def test_cmd_stats_renders_counts():
    d, sender, mx, state, directory = _build()
    directory.stats = [(DIALOGS[0], 12), (DIALOGS[1], 5)]
    await d.on_matrix_message(_text("!tg stats"))
    body = mx.deliveries[-1][1].text
    assert "Alice" in body and "12" in body and "17 条" in body
