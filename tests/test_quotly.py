"""QuotLy send mode: the `fmsg` command, the scheduler hook, and the quoter.

The quoter is exercised against a fake Telethon client, so the whole four-step
dance (ask the bot, take its sticker, send it on, clean the bot chat up) is
verified without a network.
"""

import asyncio

from bridge.adapters.outbound_scheduler import OutboundScheduler
from bridge.adapters.telegram_quotly import QuotLyQuoter
from bridge.core.dispatcher import Dispatcher
from bridge.core.models import (
    Dialog,
    InboundMessage,
    MediaRef,
    MessageKind,
    OutboundMessage,
)
from bridge.core.state import FORWARD_NORMAL, FORWARD_QUOTLY, BridgeState

from .fakes import (
    FakeAccounts,
    FakeDirectory,
    FakeExpirer,
    FakeFetcher,
    FakeSender,
    RecordingSink,
)

CONTROL = "!control:matrix.org"
DIALOGS = [Dialog(id=111, name="Alice", kind="user", username="alice")]


# -- state ---------------------------------------------------------------------


def test_forward_mode_defaults_to_normal_and_persists(tmp_path):
    path = str(tmp_path / "state.json")
    s = BridgeState(path)
    assert s.forward_mode() == FORWARD_NORMAL
    s.set_forward_mode("QuotLy")  # as typed, any case
    assert s.forward_mode() == FORWARD_QUOTLY
    assert BridgeState(path).forward_mode() == FORWARD_QUOTLY


def test_unknown_forward_mode_falls_back_to_normal():
    s = BridgeState()
    s.set_forward_mode("quotly")
    s.set_forward_mode("sparkles")
    assert s.forward_mode() == FORWARD_NORMAL


# -- the `fmsg` command --------------------------------------------------------


def _dispatcher(state=None):
    sender, mx = FakeSender(), RecordingSink()
    state = state or BridgeState()
    accounts = FakeAccounts(FakeDirectory(DIALOGS), sender, state=state)
    d = Dispatcher(accounts, mx, state, CONTROL, command_prefix="!tg",
                   timezone="UTC")
    return d, mx, state


def _mx(text, room=CONTROL):
    return InboundMessage(kind=MessageKind.TEXT, source_room=room, sender="@me",
                          text=text, event_id="$e1")


def _last(mx):
    return mx.deliveries[-1][1].text


async def test_fmsg_sets_and_reports_mode():
    d, mx, state = _dispatcher()

    await d.on_matrix_message(_mx("!tg fmsg QuotLy"))
    assert state.forward_mode() == FORWARD_QUOTLY
    assert "QuotLy" in _last(mx)

    await d.on_matrix_message(_mx("!tg fmsg"))  # no argument = show current
    assert "QuotLy" in _last(mx)

    await d.on_matrix_message(_mx("!tg fmsg normal"))
    assert state.forward_mode() == FORWARD_NORMAL


async def test_fmsg_rejects_unknown_mode():
    d, mx, state = _dispatcher()
    await d.on_matrix_message(_mx("!tg fmsg QuotLyy"))
    assert state.forward_mode() == FORWARD_NORMAL
    assert "Normal" in _last(mx) and "QuotLy" in _last(mx)


async def test_settings_shows_the_mode():
    state = BridgeState()
    state.set_forward_mode(FORWARD_QUOTLY)
    d, mx, _ = _dispatcher(state)
    await d.on_matrix_message(_mx("!tg settings"))
    assert "发送模式" in _last(mx) and "QuotLy" in _last(mx)


# -- the scheduler hook --------------------------------------------------------


class FakeQuoter:
    def __init__(self, msg_id="777") -> None:
        self.calls: list[tuple] = []
        self.msg_id = msg_id
        self.error: Exception | None = None

    async def quote_send(self, chat_id, text, reply_to=None):
        self.calls.append((chat_id, text, reply_to))
        if self.error is not None:
            raise self.error
        return self.msg_id


def _sched(tmp_path, state, quoter):
    tg = RecordingSink(return_id=555)
    return OutboundScheduler(
        tg, FakeFetcher(), state, FakeExpirer(), str(tmp_path / "outbox.json"),
        quoter=quoter, interval=0.01, clock=lambda: 1000.0, rng=lambda: 0.0,
    ), tg


def _quotly_state():
    state = BridgeState()
    state.set_forward_mode(FORWARD_QUOTLY)
    return state


async def test_quotly_mode_sends_a_sticker_instead_of_the_text(tmp_path):
    quoter = FakeQuoter()
    sched, tg = _sched(tmp_path, _quotly_state(), quoter)
    await sched.submit(111, "user",
                       OutboundMessage(MessageKind.TEXT, text="hi", reply_to=42))
    assert quoter.calls == [(111, "hi", 42)]
    assert tg.deliveries == []  # the plain send never happened


async def test_normal_mode_ignores_the_quoter(tmp_path):
    quoter = FakeQuoter()
    sched, tg = _sched(tmp_path, BridgeState(), quoter)
    await sched.submit(111, "user", OutboundMessage(MessageKind.TEXT, text="hi"))
    assert quoter.calls == []
    assert tg.deliveries[0][1].text == "hi"


async def test_media_is_never_quoted(tmp_path):
    quoter = FakeQuoter()
    sched, tg = _sched(tmp_path, _quotly_state(), quoter)
    ref = MediaRef(uri="mxc://s/9", mimetype="image/png", filename="p.png")
    await sched.submit(111, "user",
                       OutboundMessage(MessageKind.IMAGE, text="cap", media=ref))
    assert quoter.calls == []
    assert tg.deliveries[0][1].kind is MessageKind.IMAGE


async def test_quote_failure_falls_back_to_the_typed_text(tmp_path):
    quoter = FakeQuoter()
    quoter.error = RuntimeError("bot is down (test)")
    sched, tg = _sched(tmp_path, _quotly_state(), quoter)
    await sched.submit(111, "user", OutboundMessage(MessageKind.TEXT, text="hi"))
    assert tg.deliveries[0][1].text == "hi"  # delivered anyway


async def test_unrenderable_quote_falls_back_to_the_typed_text(tmp_path):
    quoter = FakeQuoter(msg_id=None)  # bot answered with no sticker
    sched, tg = _sched(tmp_path, _quotly_state(), quoter)
    await sched.submit(111, "user", OutboundMessage(MessageKind.TEXT, text="hi"))
    assert tg.deliveries[0][1].text == "hi"


async def test_sticker_id_is_what_gets_linked(tmp_path):
    from bridge.core.replymap import ReplyMap

    rm = ReplyMap()
    quoter = FakeQuoter(msg_id="777")
    tg = RecordingSink(return_id=555)
    sched = OutboundScheduler(
        tg, FakeFetcher(), _quotly_state(), FakeExpirer(),
        str(tmp_path / "outbox.json"), reply_map=rm, quoter=quoter,
        interval=0.01, clock=lambda: 1000.0, rng=lambda: 0.0,
    )
    await sched.submit(111, "user", OutboundMessage(MessageKind.TEXT, text="hi"),
                       origin_event="$evt", target_name="Alice")
    ref = rm.lookup("$evt")
    assert ref is not None and ref.msg_id == 777  # the sticker, not the text


# -- the quoter itself ---------------------------------------------------------


class FakeMessage:
    def __init__(self, msg_id, sticker=None) -> None:
        self.id = msg_id
        self.sticker = sticker
        self.document = None


class FakeConversation:
    def __init__(self, client, replies) -> None:
        self._client = client
        self._replies = list(replies)
        self._next_id = 800  # our own messages: 801, 802, …

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send_message(self, text):
        self._client.sent_to_bot.append(text)
        self._next_id += 1
        return FakeMessage(self._next_id)

    async def get_response(self):
        if not self._replies:
            raise asyncio.TimeoutError()
        return self._replies.pop(0)


class FakeClient:
    """Just enough Telethon for the quoter: a conversation, send_file, delete."""

    def __init__(self, replies) -> None:
        self._replies = replies
        self.sent_to_bot: list[str] = []
        self.sent_files: list[tuple] = []
        self.deleted: list[tuple] = []
        self.order: list[str] = []

    def conversation(self, entity, timeout=None, total_timeout=None,
                     exclusive=True):
        return FakeConversation(self, self._replies)

    async def send_file(self, entity, file, reply_to=None):
        self.order.append("send_file")
        self.sent_files.append((entity, file, reply_to))
        return FakeMessage(4242)

    async def delete_messages(self, entity, ids, revoke=False):
        self.order.append("delete")
        self.deleted.append((entity, list(ids), revoke))

    async def get_input_entity(self, who):
        return type("Peer", (), {"user_id": 5001})()


async def test_quoter_sends_the_sticker_and_cleans_the_bot_chat_up():
    sticker = object()
    client = FakeClient([FakeMessage(901, sticker=sticker)])
    q = QuotLyQuoter(client, bot="@QuotLyBot")

    msg_id = await q.quote_send(111, "hello", reply_to=7)

    assert msg_id == "4242"
    assert client.sent_to_bot == ["hello"]
    assert client.sent_files == [(111, sticker, 7)]
    # Both our message (801) and the bot's answer (901) go, for both sides.
    entity, ids, revoke = client.deleted[0]
    assert entity == "@QuotLyBot"
    assert sorted(ids) == [801, 901]
    assert revoke is True
    # The sticker is forwarded on before the messages carrying it are deleted.
    assert client.order == ["send_file", "delete"]


async def test_quoter_skips_a_reply_that_is_not_a_sticker():
    sticker = object()
    client = FakeClient([
        FakeMessage(901),                       # a text hint from the bot
        FakeMessage(902, sticker=sticker),      # then the sticker
    ])
    q = QuotLyQuoter(client, bot="@QuotLyBot")
    assert await q.quote_send(111, "hello") == "4242"
    assert client.sent_files[0][1] is sticker
    # The hint the bot sent first is cleaned up too, not just the sticker.
    assert sorted(client.deleted[0][1]) == [801, 901, 902]


async def test_quoter_gives_up_when_the_bot_stays_silent():
    client = FakeClient([])  # get_response times out
    q = QuotLyQuoter(client, bot="@QuotLyBot")
    assert await q.quote_send(111, "hello") is None
    assert client.sent_files == []
    # The message we sent the bot is still cleaned up.
    assert client.deleted and client.deleted[0][1] == [801]


async def test_quoter_never_quotes_inside_the_bots_own_chat():
    client = FakeClient([FakeMessage(901, sticker=object())])
    q = QuotLyQuoter(client, bot="@QuotLyBot")
    assert await q.quote_send(5001, "hello") is None  # 5001 = the bot itself
    assert client.sent_to_bot == []


async def test_quoter_ignores_blank_text():
    client = FakeClient([FakeMessage(901, sticker=object())])
    q = QuotLyQuoter(client, bot="@QuotLyBot")
    assert await q.quote_send(111, "   ") is None
    assert client.sent_to_bot == []
