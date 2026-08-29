"""QuotLy sticker quoter — the "fmsg QuotLy" send mode.

@QuotLyBot renders a message as a quote *sticker*. Telegram gives no API for
that, so the account has to talk to the bot like a person would:

    1. send the text to the bot,
    2. wait for the sticker it answers with,
    3. send that sticker on to the real target,
    4. delete both messages from the bot chat, leaving no trace.

Step 3 runs before step 4 deliberately: the sticker is re-sent by document
reference, and a reference is safest to use while the message carrying it is
still there. The cleanup happens in a `finally`, so a failed send never leaves
the typed text sitting in the bot chat.

Only the Telegram adapter layer can do any of this — it owns the client — so
the core sees just the `StickerQuoter` port.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telethon import TelegramClient

log = logging.getLogger(__name__)

DEFAULT_BOT = "@QuotLyBot"

# The bot sometimes answers with a text line ("send me a message to quote…")
# before, or instead of, the sticker. Read a couple of replies before giving
# up, still bounded by the overall timeout.
_MAX_REPLIES = 3


class QuotLyQuoter:
    """Turns text into a QuotLy sticker in the target chat."""

    def __init__(
        self,
        client: TelegramClient,
        bot: str = DEFAULT_BOT,
        timeout: float = 30.0,
    ):
        self._client = client
        self._bot = (bot or DEFAULT_BOT).strip() or DEFAULT_BOT
        self._timeout = timeout
        # One quote at a time per account: two overlapping conversations with
        # the same bot would race for each other's replies, and the sticker
        # that came back for message A must not be sent as message B.
        self._lock = asyncio.Lock()
        self._bot_id: Optional[int] = None

    async def quote_send(
        self, chat_id: int, text: str, reply_to: Optional[int] = None
    ) -> Optional[str]:
        body = (text or "").strip()
        if not body:
            return None
        async with self._lock:
            try:
                bot_id = await self._resolve_bot()
            except Exception:  # noqa: BLE001 - unknown bot -> plain send
                log.warning("QuotLy: cannot resolve %s", self._bot, exc_info=True)
                return None
            if bot_id is not None and int(chat_id) == bot_id:
                # Quoting inside the bot's own chat would mean deleting the
                # very message we just delivered.
                return None
            return await self._render_and_send(int(chat_id), body, reply_to)

    async def _render_and_send(
        self, chat_id: int, text: str, reply_to: Optional[int]
    ) -> Optional[str]:
        sent = None
        seen: list = []  # every message in the bot chat, so all of it is cleaned
        try:
            async with self._client.conversation(
                self._bot,
                timeout=self._timeout,
                # Bounds the whole exchange, not just one reply: a bot that
                # answers three times without a sticker must not hold the
                # send queue for three full timeouts.
                total_timeout=self._timeout,
                exclusive=False,
            ) as conv:
                sent = await conv.send_message(text)
                sticker = None
                for _ in range(_MAX_REPLIES):
                    reply = await conv.get_response()
                    seen.append(reply)
                    sticker = _sticker_of(reply)
                    if sticker is not None:
                        break
                if sticker is None:
                    log.warning("QuotLy: %s answered without a sticker", self._bot)
                    return None
            out = await self._client.send_file(
                chat_id, sticker, reply_to=reply_to
            )
            msg_id = getattr(out, "id", None)
            return str(msg_id) if msg_id is not None else None
        except asyncio.TimeoutError:
            log.warning("QuotLy: %s did not answer in %ss", self._bot, self._timeout)
            return None
        finally:
            await self._cleanup([sent, *seen])

    async def _cleanup(self, messages: list) -> None:
        """Delete our message to the bot and everything it answered, both sides.

        Best-effort by design: a failure here must not lose a message that was
        already delivered, so it is logged and swallowed.
        """
        ids = [m.id for m in messages if getattr(m, "id", None) is not None]
        if not ids:
            return
        try:
            await self._client.delete_messages(self._bot, ids, revoke=True)
        except Exception:  # noqa: BLE001
            log.warning("QuotLy: could not clean up %s in %s", ids, self._bot,
                        exc_info=True)

    async def _resolve_bot(self) -> Optional[int]:
        """The bot's numeric id, cached. None if the client can't tell us."""
        if self._bot_id is None:
            entity = await self._client.get_input_entity(self._bot)
            bot_id = getattr(entity, "user_id", None) or getattr(entity, "id", None)
            self._bot_id = int(bot_id) if bot_id is not None else None
        return self._bot_id


def _sticker_of(message):
    """The sticker document of a bot reply, or None if it isn't one."""
    if message is None:
        return None
    return getattr(message, "sticker", None) or getattr(message, "document", None)
