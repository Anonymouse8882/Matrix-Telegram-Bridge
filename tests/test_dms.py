"""DM listing/reading commands, and the @username routing they depend on."""

from datetime import datetime, timezone

import pytest

from bridge.core.dispatcher import Dispatcher
from bridge.core.models import Dialog, DialogSummary, InboundMessage, MessageKind
from bridge.core.state import BridgeState

from .fakes import FakeAccounts, FakeDirectory, FakeSender, RecordingSink

ROOM = "!room:matrix.org"

DIALOGS = [
    Dialog(id=111, name="Alice", kind="user", username="alice"),
    Dialog(id=222, name="Zhan Wo", kind="user", username="zhanwo"),
    # Same display name as a contact - must never win a DM-scoped lookup.
    Dialog(id=-333, name="Zhan Wo", kind="group", username="zhanwo"),
]


def _epoch(day, hour):
    return datetime(2026, 7, day, hour, 0, tzinfo=timezone.utc).timestamp()


def _build(active=None, muted=()):
    sender, mx = FakeSender(), RecordingSink()
    directory = FakeDirectory(DIALOGS)
    state = BridgeState()
    accounts = FakeAccounts(directory, sender, state=state)
    if active:
        state.set_active_target("@current", active)
    for m in muted:
        state.mute(m)
    d = Dispatcher(accounts, mx, state, ROOM,
                   command_prefix="!tg", timezone="UTC")
    return d, sender, mx, state, directory


def _text(body):
    return InboundMessage(MessageKind.TEXT, ROOM, "@bot", text=body)


def _last(mx):
    return mx.deliveries[-1][1].text


# -- the @username routing bug -------------------------------------------------


async def test_at_prefix_resolves_a_username():
    """`@zhanwo no` used to strip the @, so only display names ever matched."""
    d, sender, mx, _, _ = _build(active="111")
    await d.on_matrix_message(_text("@zhanwo no"))
    chat_id, kind, message, _at = sender.submissions[0]
    assert chat_id == 222
    assert message.text == "no"


async def test_at_prefix_keeps_the_at_for_lookup():
    d, _s, _mx, _st, directory = _build(active="111")
    await d.on_matrix_message(_text("@zhanwo hi"))
    assert directory.resolve_calls[0][0] == "@zhanwo"


async def test_at_prefix_passes_numeric_ids_bare():
    """"@123" is not a username - stripping it back keeps id targeting working."""
    d, sender, _mx, _st, directory = _build(active="111")
    await d.on_matrix_message(_text("@222 hi"))
    assert directory.resolve_calls[0][0] == "222"
    assert sender.submissions[0][0] == 222


async def test_at_prefix_still_matches_display_names():
    d, sender, _mx, _st, _ = _build(active="222")
    await d.on_matrix_message(_text("@Alice hello"))
    assert sender.submissions[0][0] == 111


async def test_unknown_at_target_reports_and_sends_nothing():
    d, sender, mx, _, _ = _build(active="111")
    await d.on_matrix_message(_text("@nobody hi"))
    assert sender.submissions == []
    assert "找不到目标" in _last(mx)


async def test_at_prefix_without_body_is_usage():
    d, sender, mx, _, _ = _build(active="111")
    await d.on_matrix_message(_text("@zhanwo"))
    assert sender.submissions == []
    assert "用法" in _last(mx)


# -- !tg dms -------------------------------------------------------------------


async def test_dms_lists_private_chats_with_previews():
    d, _s, mx, _st, directory = _build()
    directory.dms = [
        DialogSummary(DIALOGS[0], unread=3, last_text="see you",
                      last_date=_epoch(20, 9)),
        DialogSummary(DIALOGS[1], last_text="ok", last_outgoing=True,
                      last_date=_epoch(19, 8)),
    ]
    await d.on_matrix_message(_text("!tg dms"))
    body = _last(mx)
    assert "Alice" in body and "@alice" in body
    assert "🔴3" in body          # unread badge
    assert "see you" in body
    assert "我: ok" in body       # outgoing messages are marked as yours


async def test_dms_reports_media_only_messages():
    d, _s, mx, _st, directory = _build()
    directory.dms = [DialogSummary(DIALOGS[0], last_media=True,
                                   last_date=_epoch(20, 9))]
    await d.on_matrix_message(_text("!tg dms"))
    assert "[媒体]" in _last(mx)


async def test_dms_marks_active_and_muted():
    d, _s, mx, _st, directory = _build(active="111", muted=["222"])
    directory.dms = [DialogSummary(DIALOGS[0]), DialogSummary(DIALOGS[1])]
    await d.on_matrix_message(_text("!tg dms"))
    body = _last(mx)
    assert "⭐" in body and "🔕" in body


async def test_dms_truncates_and_says_so():
    d, _s, mx, _st, directory = _build()
    directory.dms = [
        DialogSummary(Dialog(id=i, name=f"U{i}", kind="user")) for i in range(40)
    ]
    await d.on_matrix_message(_text("!tg dms 5"))
    body = _last(mx)
    assert "U4" in body and "U9" not in body
    assert "共 40 个" in body


async def test_dms_empty():
    d, _s, mx, _st, directory = _build()
    directory.dms = []
    await d.on_matrix_message(_text("!tg dms"))
    assert "没有任何私信" in _last(mx)


async def test_long_preview_is_truncated():
    d, _s, mx, _st, directory = _build()
    directory.dms = [DialogSummary(DIALOGS[0], last_text="x" * 200)]
    await d.on_matrix_message(_text("!tg dms"))
    assert "…" in _last(mx)


async def test_preview_collapses_newlines():
    """A multi-line message must not break the one-line-per-chat layout."""
    d, _s, mx, _st, directory = _build()
    directory.dms = [DialogSummary(DIALOGS[0], last_text="line one\nline two")]
    await d.on_matrix_message(_text("!tg dms"))
    assert "line one line two" in _last(mx)


# -- !tg dm <target> -----------------------------------------------------------


async def test_dm_reads_a_conversation():
    d, _s, mx, _st, directory = _build()
    directory.history_rows = [("Alice", "hi"), ("me", "hello")]
    await d.on_matrix_message(_text("!tg dm alice"))
    body = _last(mx)
    assert "Alice: hi" in body and "me: hello" in body


async def test_dm_lookup_is_scoped_to_private_chats():
    """A group with the same name/username must not be picked for `dm`."""
    d, _s, mx, _st, directory = _build()
    directory.history_rows = [("Zhan Wo", "yo")]
    await d.on_matrix_message(_text("!tg dm zhanwo"))
    assert directory.resolve_calls[-1] == ("zhanwo", "user")
    assert "Zhan Wo" in _last(mx)


async def test_dm_without_target_is_usage():
    d, _s, mx, _st, _ = _build()
    await d.on_matrix_message(_text("!tg dm"))
    assert "用法" in _last(mx)


async def test_dm_unknown_target_points_at_dms():
    d, _s, mx, _st, _ = _build()
    await d.on_matrix_message(_text("!tg dm nobody"))
    body = _last(mx)
    assert "找不到私信对象" in body and "dms" in body


@pytest.mark.parametrize("cmd,expected", [
    ("!tg dm alice", 20),      # default
    ("!tg dm alice 5", 5),
    ("!tg dm alice 999", 50),  # clamped
    ("!tg dm alice abc", 20),  # unparseable falls back to the default
])
async def test_dm_count_handling(cmd, expected):
    d, _s, _mx, _st, directory = _build()
    directory.history_rows = [("a", str(i)) for i in range(60)]
    await d.on_matrix_message(_text(cmd))
    assert directory.history_limits[-1] == expected


async def test_dm_empty_history():
    d, _s, mx, _st, directory = _build()
    directory.history_rows = []
    await d.on_matrix_message(_text("!tg dm alice"))
    assert "没有可显示的消息" in _last(mx)


async def test_help_mentions_the_new_commands():
    d, _s, mx, _st, _ = _build()
    await d.on_matrix_message(_text("!tg help"))
    body = _last(mx)
    # panel() renders HTML, so angle brackets arrive escaped.
    assert "dms" in body and "dm &lt;目标&gt;" in body
