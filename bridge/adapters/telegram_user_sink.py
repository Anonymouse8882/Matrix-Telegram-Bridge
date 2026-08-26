"""Telegram *user-account* sink adapter (MTProto via Telethon).

Sends as a real Telegram account, not a bot — so it can DM anyone the account
can reach and post to any group/channel the account belongs to, without the
Bot API's restrictions. Implements the exact same `MessageSink` port as the
bot adapter, so the bridge core is completely unaffected by the swap.

Auth uses a Telethon *session file* created once via `python -m bridge.tglogin`
(the interactive phone-code login can't run unattended in a container).
"""

from __future__ import annotations

import io
import logging

from telethon import TelegramClient

from ..core.models import MessageKind, OutboundMessage, TelegramTarget
from ..core.ports import MessageSink

log = logging.getLogger(__name__)

# Kinds we deliberately send as a plain document rather than letting Telegram
# auto-render them (stickers-as-webp and arbitrary files are more reliable so).
_FORCE_DOCUMENT = {MessageKind.STICKER, MessageKind.FILE}


class TelegramUserSink(MessageSink):
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session: str = "telegram.session",
        phone: str = "",
        client: TelegramClient | None = None,
    ):
        # Injectable client keeps this unit-testable with a fake (no network).
        self._client = client or TelegramClient(session, api_id, api_hash)
        self._phone = phone
        self._ready = False

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        if not self._client.is_connected():
            await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError(
                "Telegram user session is not authorized. Run "
                "`python -m bridge.tglogin --config config.yaml` once to log in."
            )
        self._ready = True

    async def deliver(
        self, target: TelegramTarget, message: OutboundMessage
    ) -> str | None:
        await self._ensure_ready()
        entity = _coerce_peer(target.chat_id)
        # reply_to targets a specific message; message_thread_id a forum topic.
        reply_to = target.reply_to or target.message_thread_id

        # Owner-typed content is sent literally (parse_mode=None) so characters
        # like * _ < aren't reinterpreted as formatting.
        parse_mode = "html" if message.html else None

        if message.kind is MessageKind.TEXT or not message.media_bytes:
            text = message.text or ""
            if not text:
                return None  # nothing to send
            sent = await self._client.send_message(
                entity, text, parse_mode=parse_mode, reply_to=reply_to,
                link_preview=False,
            )
            return _id_str(sent)

        stream = io.BytesIO(message.media_bytes)
        stream.name = message.filename or f"file{_ext(message.mimetype)}"
        sent = await self._client.send_file(
            entity,
            stream,
            caption=message.text or None,
            parse_mode=parse_mode,
            reply_to=reply_to,
            force_document=message.kind in _FORCE_DOCUMENT,
        )
        return _id_str(sent)

    async def close(self) -> None:
        if self._client.is_connected():
            await self._client.disconnect()


def _id_str(sent) -> str | None:
    mid = getattr(sent, "id", None)
    return str(mid) if mid is not None else None


def _coerce_peer(chat_id: str):
    """Turn a configured chat_id into something Telethon can resolve.

    Numeric ids (users, and channels like -100…) become ints; @usernames,
    t.me links and phone numbers stay strings for Telethon to look up.
    """
    s = chat_id.strip()
    if s.startswith("@") or s.startswith("+") or s.startswith("http"):
        return s
    try:
        return int(s)
    except ValueError:
        return s


def _ext(mimetype: str | None) -> str:
    if not mimetype or "/" not in mimetype:
        return ".bin"
    return "." + mimetype.split("/", 1)[1].split(";")[0]
