"""OutboundScheduler tests: immediate / delayed / scheduled + self-destruct."""

import pytest

from bridge.adapters.outbound_scheduler import OutboundScheduler
from bridge.core.models import MediaRef, MessageKind, OutboundMessage
from bridge.core.replymap import ReplyMap
from bridge.core.state import BridgeState

from .fakes import FakeExpirer, FakeFetcher, RecordingSink


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _build(tmp_path, state=None, reply_map=None, control_room=""):
    tg = RecordingSink(return_id=555)
    fetcher = FakeFetcher()
    state = state or BridgeState()
    expirer = FakeExpirer()
    clock = Clock()
    sched = OutboundScheduler(
        tg, fetcher, state, expirer, str(tmp_path / "outbox.json"),
        reply_map=reply_map, control_room=control_room,
        interval=0.01, clock=clock, rng=lambda: 0.0,
    )
    return sched, tg, fetcher, state, expirer, clock


def _text(body="hi"):
    return OutboundMessage(MessageKind.TEXT, text=body)


async def test_immediate_send_when_no_delay(tmp_path):
    sched, tg, _, _, _, _ = _build(tmp_path)
    status, when = await sched.submit(111, "user", _text("yo"))
    assert status == "sent" and when is None
    assert tg.deliveries[0][0].chat_id == "111"
    assert tg.deliveries[0][1].text == "yo"


async def test_delay_defers_then_sweep_delivers(tmp_path):
    state = BridgeState()
    state.set_delay(10, 0)
    sched, tg, _, _, _, clock = _build(tmp_path, state)

    status, when = await sched.submit(111, "user", _text())
    assert status == "scheduled"
    assert when == 1010.0            # now(1000) + fixed(10)
    assert tg.deliveries == []        # not sent yet

    clock.t = 1005.0
    await sched._sweep()
    assert tg.deliveries == []        # still early

    clock.t = 1011.0
    await sched._sweep()
    assert len(tg.deliveries) == 1    # now delivered


async def test_scheduled_at_absolute_time(tmp_path):
    sched, tg, _, _, _, clock = _build(tmp_path)
    status, when = await sched.submit(111, "user", _text(), at=1500.0)
    assert status == "scheduled" and when == 1500.0
    clock.t = 1499.0
    await sched._sweep()
    assert tg.deliveries == []
    clock.t = 1501.0
    await sched._sweep()
    assert len(tg.deliveries) == 1


async def test_self_destruct_scheduled_after_send(tmp_path):
    state = BridgeState()
    state.set_self_destruct("user", 60)
    sched, tg, _, _, expirer, _ = _build(tmp_path, state)
    await sched.submit(111, "user", _text())
    assert expirer.scheduled == [(111, 555, 60)]


async def test_media_is_fetched_at_send_time(tmp_path):
    sched, tg, fetcher, _, _, _ = _build(tmp_path)
    ref = MediaRef(uri="mxc://s/9", mimetype="image/png", filename="p.png")
    await sched.submit(
        -333, "group",
        OutboundMessage(MessageKind.IMAGE, text="cap", media=ref),
    )
    assert fetcher.calls == [ref]
    sent = tg.deliveries[0][1]
    assert sent.kind is MessageKind.IMAGE
    assert sent.media_bytes == b"BYTES"
    assert sent.filename == "p.png"


async def test_send_records_reply_mapping(tmp_path):
    rm = ReplyMap()
    sched, tg, _, _, _, _ = _build(tmp_path, reply_map=rm)
    await sched.submit(111, "user", _text("hi"), origin_event="$evt", target_name="Alice")
    ref = rm.lookup("$evt")
    assert ref is not None
    assert ref.chat_id == 111 and ref.msg_id == 555 and ref.name == "Alice"


async def test_self_destruct_carries_matrix_event(tmp_path):
    state = BridgeState()
    state.set_self_destruct("user", 60)
    sched, tg, _, _, expirer, _ = _build(tmp_path, state, control_room="!room")
    await sched.submit(111, "user", _text(), origin_event="$evt")
    assert expirer.scheduled == [(111, 555, 60)]
    assert expirer.matrix == [("!room", "$evt")]  # deletes Matrix copy too


async def test_reply_to_flows_to_target(tmp_path):
    sched, tg, _, _, _, _ = _build(tmp_path)
    await sched.submit(
        111, "user", OutboundMessage(MessageKind.TEXT, text="re", reply_to=42)
    )
    target = tg.deliveries[0][0]
    assert target.reply_to == 42


class FailingSink:
    """A sink that fails a set number of times before succeeding."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempts = 0
        self.delivered = 0

    async def deliver(self, target, message):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise RuntimeError("transient network error (test)")
        self.delivered += 1
        return 555

    async def close(self) -> None:
        pass


async def test_failed_scheduled_send_is_retried_not_dropped(tmp_path):
    tg = FailingSink(failures=1)
    clock = Clock()
    sched = OutboundScheduler(
        tg, FakeFetcher(), BridgeState(), FakeExpirer(),
        str(tmp_path / "outbox.json"), interval=0.01, clock=clock,
        rng=lambda: 0.0,
    )
    await sched.submit(111, "user", _text("keep me"), at=1500.0)

    clock.t = 1501.0
    await sched._sweep()          # first attempt fails
    assert tg.delivered == 0
    assert len(sched._pending) == 1  # still queued, not dropped

    clock.t = 2000.0              # past the retry backoff
    await sched._sweep()
    assert tg.delivered == 1
    assert sched._pending == []


async def test_permanently_failing_send_is_dropped_after_max_attempts(tmp_path):
    tg = FailingSink(failures=99)
    clock = Clock()
    sched = OutboundScheduler(
        tg, FakeFetcher(), BridgeState(), FakeExpirer(),
        str(tmp_path / "outbox.json"), interval=0.01, clock=clock,
        rng=lambda: 0.0,
    )
    await sched.submit(111, "user", _text(), at=1500.0)
    for _ in range(10):           # far more sweeps than allowed attempts
        clock.t += 10_000.0
        await sched._sweep()
    assert sched._pending == []   # gave up eventually
    assert tg.attempts == 5       # exactly _MAX_ATTEMPTS tries


async def test_deferred_send_persists_across_instances(tmp_path):
    state = BridgeState()
    state.set_delay(100, 0)
    sched, tg, fetcher, _, expirer, clock = _build(tmp_path, state)
    await sched.submit(111, "user", _text("later"))

    # A fresh scheduler (same file) loads the pending item and delivers it.
    tg2 = RecordingSink(return_id=1)
    clock2 = Clock(t=2000.0)  # well past send_at
    sched2 = OutboundScheduler(
        tg2, fetcher, state, expirer, str(tmp_path / "outbox.json"),
        interval=0.01, clock=clock2, rng=lambda: 0.0,
    )
    await sched2._sweep()
    assert len(tg2.deliveries) == 1
    assert tg2.deliveries[0][1].text == "later"
