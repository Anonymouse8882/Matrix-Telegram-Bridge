"""Telegram source: how a raw Telethon message's reply/forward headers are read."""

from types import SimpleNamespace

from telethon.tl.types import Channel, User

from bridge.adapters.telegram_user_source import (
    _forward_label,
    _forward_origin,
    _reply_to_msg_id,
)
from bridge.core.transformer import UNKNOWN_ORIGIN


def _msg(reply_to=None):
    return SimpleNamespace(reply_to=reply_to)


def test_no_reply_header():
    assert _reply_to_msg_id(_msg()) is None


def test_plain_reply():
    header = SimpleNamespace(reply_to_msg_id=42, forum_topic=False,
                             reply_to_top_id=None)
    assert _reply_to_msg_id(_msg(header)) == 42


def test_forum_topic_root_is_not_a_reply():
    """Every forum message points at its topic; that is not a reply."""
    header = SimpleNamespace(reply_to_msg_id=7, forum_topic=True,
                             reply_to_top_id=None)
    assert _reply_to_msg_id(_msg(header)) is None


def test_real_reply_inside_a_forum_topic():
    header = SimpleNamespace(reply_to_msg_id=42, forum_topic=True,
                             reply_to_top_id=7)
    assert _reply_to_msg_id(_msg(header)) == 42


# -- forward headers ---------------------------------------------------------


class _Forward:
    """A Telethon forward header: one of sender / chat resolves, or neither."""

    def __init__(self, sender=None, chat=None, raises=False):
        self._sender, self._chat, self._raises = sender, chat, raises

    async def get_sender(self):
        if self._raises:
            raise RuntimeError("privacy settings")
        return self._sender

    async def get_chat(self):
        if self._raises:
            raise RuntimeError("privacy settings")
        return self._chat


def _channel(title):
    """A real Telethon type: `get_display_name` dispatches on the class."""
    return Channel(id=1, title=title, photo=None, date=None)


def _fwd_msg(header, forward):
    return SimpleNamespace(fwd_from=header, forward=forward)


async def test_a_plain_message_has_no_origin():
    assert await _forward_origin(_fwd_msg(None, None)) is None


async def test_forward_from_a_person():
    header = SimpleNamespace(from_name=None, post_author=None)
    msg = _fwd_msg(header, _Forward(sender=User(id=7, first_name="Carol")))
    assert await _forward_origin(msg) == "Carol"


async def test_forward_from_a_channel():
    header = SimpleNamespace(from_name=None, post_author=None)
    chat = _channel("某频道")
    assert await _forward_origin(_fwd_msg(header, _Forward(chat=chat))) == "某频道"


async def test_forward_from_a_signed_channel_post():
    header = SimpleNamespace(from_name=None, post_author="张三")
    chat = _channel("某频道")
    assert await _forward_origin(_fwd_msg(header, _Forward(chat=chat))) == \
        "某频道 · 张三"


async def test_hidden_account_falls_back_to_the_name_telegram_gives():
    """An account that forbids being linked still supplies a display name."""
    header = SimpleNamespace(from_name="李四", post_author=None)
    assert await _forward_origin(_fwd_msg(header, _Forward())) == "李四"


async def test_an_unresolvable_origin_still_reports_the_forward():
    header = SimpleNamespace(from_name=None, post_author=None)
    msg = _fwd_msg(header, _Forward(raises=True))
    assert await _forward_origin(msg) == UNKNOWN_ORIGIN


async def test_forward_label_does_not_repeat_an_identical_author():
    assert _forward_label(None, "某频道", None, "某频道") == "某频道"
