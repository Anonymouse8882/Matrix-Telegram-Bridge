"""Self-destruct: the Telegram message goes, the Matrix copy gets marked."""

import json
import os

import pytest

from bridge.adapters.telegram_expirer import TelegramExpirer


class FakeTgClient:
    def __init__(self, fail=False):
        self.deleted: list[tuple] = []
        self.fail = fail

    async def delete_messages(self, chat, ids, revoke=False):
        if self.fail:
            raise RuntimeError("no permission")
        self.deleted.append((chat, tuple(ids), revoke))


def _expirer(tmp_path, client=None, marker=None, clock=None):
    exp = TelegramExpirer(client or FakeTgClient(), str(tmp_path / "expire.json"))
    if marker is not None:
        exp.set_marker(marker)
    return exp


def _marker():
    calls: list[tuple] = []

    async def mark(chat_id, msg_ids):
        calls.append((chat_id, list(msg_ids)))

    return mark, calls


async def test_due_message_is_deleted_on_telegram(tmp_path):
    client = FakeTgClient()
    exp = _expirer(tmp_path, client)
    await exp.schedule(111, 42, delay_seconds=-1)  # already overdue

    await exp._sweep()

    assert client.deleted == [(111, (42,), True)]


async def test_matrix_copy_is_marked_not_redacted(tmp_path):
    """The record has to stay readable — that is the point of the bridge."""
    mark, calls = _marker()
    exp = _expirer(tmp_path, marker=mark)
    await exp.schedule(111, 42, delay_seconds=-1, matrix_room="!r:hs",
                       matrix_event="$e")

    await exp._sweep()

    assert calls == [(111, [42])]


async def test_marking_uses_the_same_path_as_a_remote_deletion(tmp_path):
    """Signature check: the marker IS Relay.on_telegram_deleted."""
    from bridge.core.relay import Relay

    mark, calls = _marker()
    exp = _expirer(tmp_path, marker=mark)
    await exp.schedule(-100222, 7, delay_seconds=-1)
    await exp._sweep()

    chat_id, msg_ids = calls[0]
    # Same shape the relay's handler takes: (chat | None, [msg_id, ...]).
    assert isinstance(msg_ids, list)
    assert Relay.on_telegram_deleted.__code__.co_argcount == 3


async def test_not_yet_due_is_left_alone(tmp_path):
    client = FakeTgClient()
    mark, calls = _marker()
    exp = _expirer(tmp_path, client, marker=mark)
    await exp.schedule(111, 42, delay_seconds=3600)

    await exp._sweep()

    assert client.deleted == [] and calls == []


async def test_matrix_is_still_marked_when_telegram_deletion_fails(tmp_path):
    """Already gone on Telegram is still gone — the copy should say so."""
    mark, calls = _marker()
    exp = _expirer(tmp_path, FakeTgClient(fail=True), marker=mark)
    await exp.schedule(111, 42, delay_seconds=-1)

    await exp._sweep()

    assert calls == [(111, [42])]


async def test_a_failing_marker_does_not_stall_the_sweeper(tmp_path):
    async def boom(chat_id, msg_ids):
        raise RuntimeError("matrix down")

    client = FakeTgClient()
    exp = _expirer(tmp_path, client, marker=boom)
    await exp.schedule(111, 42, delay_seconds=-1)

    await exp._sweep()  # must not raise

    assert client.deleted  # the Telegram side still happened
    assert exp._pending == []


async def test_handled_entries_are_dropped_and_persisted(tmp_path):
    path = str(tmp_path / "expire.json")
    exp = TelegramExpirer(FakeTgClient(), path)
    await exp.schedule(111, 42, delay_seconds=-1)

    await exp._sweep()

    assert exp._pending == []
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh) == []


async def test_pending_survive_a_restart(tmp_path):
    """TTLs can be days, so the queue outlives the process."""
    path = str(tmp_path / "expire.json")
    exp = TelegramExpirer(FakeTgClient(), path)
    await exp.schedule(111, 42, delay_seconds=3600)

    fresh = TelegramExpirer(FakeTgClient(), path)
    assert len(fresh._pending) == 1 and fresh._pending[0]["msg"] == 42
