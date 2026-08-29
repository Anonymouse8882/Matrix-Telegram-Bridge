"""Outbound send scheduler — the single path for Matrix -> Telegram sends.

Handles, in one place:
  * immediate delivery (no delay configured, no scheduled time),
  * global send delay  (fixed + uniform(0, random) seconds), and
  * scheduled delivery at an absolute time (`!tg at`),
then applies per-kind self-destruct after the message actually goes out.

The account's forward mode is honoured here too: in QuotLy mode plain text is
handed to the quoter, which puts a quote sticker in the chat instead.

Deferred sends are persisted (media is stored as a Matrix reference and
re-fetched at send time, not held as bytes) so they survive a restart; a
background sweeper delivers anything due.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import replace
from typing import Callable, Optional

from ..core.messagelinks import MessageLinks
from ..core.models import MediaRef, MessageKind, OutboundMessage, TelegramTarget
from ..core.ports import MediaFetcher, MessageExpirer, MessageSink, StickerQuoter
from ..core.replymap import ReplyMap, ReplyRef
from ..core.state import FORWARD_QUOTLY, BridgeState
from .jsonfile import load_json_list, save_json_list

log = logging.getLogger(__name__)

# A queued send that keeps failing is retried with a growing backoff, then
# dropped: a scheduled message must survive a network blip, but a permanently
# refused one must not clog the queue forever.
_MAX_ATTEMPTS = 5
_RETRY_BASE = 30.0  # seconds; attempt n waits n * this


class OutboundScheduler:
    def __init__(
        self,
        telegram_sink: MessageSink,
        matrix_fetcher: MediaFetcher,
        state: BridgeState,
        expirer: MessageExpirer,
        path: str,
        reply_map: ReplyMap | None = None,
        control_room: str = "",
        links: MessageLinks | None = None,
        quoter: StickerQuoter | None = None,
        interval: float = 5.0,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
    ):
        self._tg = telegram_sink
        self._fetcher = matrix_fetcher
        self._state = state
        self._expirer = expirer
        self._path = path
        self._reply_map = reply_map
        self._control_room = control_room
        self._links = links
        self._quoter = quoter  # None = QuotLy mode unavailable, always plain
        self._interval = interval
        self._now = clock
        self._rng = rng
        self._pending: list[dict] = []
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()  # set on submit, so short delays are exact
        self._pending = load_json_list(self._path)

    def _effective_send_at(self, at: Optional[float]) -> float:
        if at is not None:
            return at
        fixed = self._state.delay_fixed()
        rnd = self._state.delay_random()
        extra = self._rng() * rnd if rnd > 0 else 0
        return self._now() + fixed + extra

    async def submit(
        self,
        chat_id: int,
        dialog_kind: str,
        message: OutboundMessage,
        at: Optional[float] = None,
        origin_event: Optional[str] = None,
        target_name: str = "",
        origin_room: Optional[str] = None,
    ) -> tuple[str, Optional[float]]:
        send_at = self._effective_send_at(at)
        if send_at <= self._now() + 0.5:
            await self._deliver(
                chat_id, dialog_kind, message, origin_event, target_name, origin_room
            )
            return ("sent", None)
        async with self._lock:
            self._pending.append(
                _encode(chat_id, dialog_kind, message, send_at, origin_event,
                        target_name, origin_room)
            )
            self._save()
        self._wake.set()  # let the sweeper re-plan its sleep for this item
        return ("scheduled", send_at)

    async def _deliver(
        self,
        chat_id: int,
        dialog_kind: str,
        message: OutboundMessage,
        origin_event: Optional[str] = None,
        target_name: str = "",
        origin_room: Optional[str] = None,
    ) -> None:
        outbound = message
        if message.media is not None and not message.media_bytes:
            data = await self._fetcher.fetch(message.media)
            # `replace` keeps every other field (silent, html, reply_to, …)
            # instead of enumerating them and silently losing one.
            outbound = replace(
                message,
                media_bytes=data,
                filename=message.media.filename,
                mimetype=message.media.mimetype,
            )
        msg_id = await self._send(chat_id, outbound)
        if not msg_id:
            return
        # Map the originating Matrix event to this TG message so the owner can
        # reply to / delete their own sent message from Element.
        if self._reply_map is not None and origin_event:
            self._reply_map.remember(
                origin_event,
                ReplyRef(chat_id, int(msg_id), dialog_kind, target_name or str(chat_id)),
            )
        # Link it for deletion sync too: deleting this message on the Telegram
        # app should redact the Matrix message the owner typed.
        if self._links is not None and origin_event and origin_room:
            self._links.add(chat_id, int(msg_id), dialog_kind, origin_room,
                            origin_event, message.text or "",
                            media=message.media is not None)
        ttl = self._state.self_destruct(dialog_kind)
        if self._expirer and ttl > 0:
            # Self-destruct deletes the TG message AND its Matrix counterpart.
            # The Matrix event lives in whatever room it was typed in (a
            # per-chat room, or the control room) — redact THAT room.
            await self._expirer.schedule(
                chat_id, int(msg_id), ttl,
                matrix_room=origin_room or self._control_room or None,
                matrix_event=origin_event,
            )

    async def _send(self, chat_id: int, message: OutboundMessage) -> Optional[str]:
        """Hand the message to Telegram the way the forward mode asks for.

        QuotLy mode only applies to plain typed text; media, and a quote the
        bot could not produce, fall back to the ordinary send. An unstyled
        message is a much smaller failure than a missing one.
        """
        if self._use_quotly(message):
            try:
                msg_id = await self._quoter.quote_send(
                    chat_id, message.text or "", reply_to=message.reply_to
                )
            except Exception:  # noqa: BLE001 - never lose the message over styling
                log.exception("quotly render failed for %s; sending as typed",
                              chat_id)
                msg_id = None
            if msg_id:
                return msg_id
            log.info("quotly unavailable for %s; sending as typed", chat_id)
        target = TelegramTarget(chat_id=str(chat_id), reply_to=message.reply_to)
        return await self._tg.deliver(target, message)

    def _use_quotly(self, message: OutboundMessage) -> bool:
        return (
            self._quoter is not None
            and self._state.forward_mode() == FORWARD_QUOTLY
            and message.kind is MessageKind.TEXT
            and not message.media_bytes
            and bool((message.text or "").strip())
        )

    def clear(self) -> None:
        """Drop every queued send (Matrix account change).

        Queued items hold the typed text and the Matrix room it came from, and
        their media is a reference only the old account can resolve — so they
        cannot be delivered after the switch anyway.
        """
        self._pending = []
        self._save()

    async def run(self) -> None:
        log.info("outbound scheduler running (%d queued)", len(self._pending))
        while True:
            await self._sweep()
            try:
                # Sleep exactly until the next item is due (capped by the base
                # interval); a submit wakes us early so short delays are exact.
                await asyncio.wait_for(self._wake.wait(), timeout=self._next_delay())
            except asyncio.TimeoutError:
                pass
            else:
                self._wake.clear()

    def _next_delay(self) -> float:
        if not self._pending:
            return self._interval
        soonest = min(p["send_at"] for p in self._pending)
        return min(self._interval, max(0.05, soonest - self._now()))

    async def _sweep(self) -> None:
        now = self._now()
        async with self._lock:
            due = [p for p in self._pending if p["send_at"] <= now]
        if not due:
            return
        finished: set[int] = set()  # by identity: items are mutable dicts
        for p in due:
            try:
                chat_id, dialog_kind, message, origin, name, room = _decode(p)
                await self._deliver(chat_id, dialog_kind, message, origin, name, room)
                finished.add(id(p))
            except Exception:  # noqa: BLE001 - retry transient failures
                attempts = int(p.get("attempts", 0)) + 1
                if attempts >= _MAX_ATTEMPTS:
                    log.exception(
                        "scheduled send for %s failed %d times; dropping it",
                        p.get("chat_id"), attempts,
                    )
                    finished.add(id(p))
                else:
                    log.exception(
                        "scheduled send for %s failed (attempt %d/%d); will retry",
                        p.get("chat_id"), attempts, _MAX_ATTEMPTS,
                    )
                    p["attempts"] = attempts
                    p["send_at"] = self._now() + _RETRY_BASE * attempts
        async with self._lock:
            self._pending = [p for p in self._pending if id(p) not in finished]
            self._save()

    # -- persistence ---------------------------------------------------------

    def _save(self) -> None:
        save_json_list(self._path, self._pending, label="outbox")


def _encode(chat_id, dialog_kind, message: OutboundMessage, send_at,
            origin_event, target_name, origin_room=None) -> dict:
    media = None
    if message.media is not None:
        media = {
            "uri": message.media.uri,
            "mimetype": message.media.mimetype,
            "filename": message.media.filename,
            "size": message.media.size,
        }
    return {
        "chat_id": int(chat_id),
        "dialog_kind": dialog_kind,
        "kind": message.kind.value,
        "text": message.text,
        "html": message.html,
        "media": media,
        "reply_to": message.reply_to,
        "origin_event": origin_event,
        "target_name": target_name,
        "origin_room": origin_room,
        "send_at": float(send_at),
    }


def _decode(p: dict):
    media = None
    if p.get("media"):
        media = MediaRef(**p["media"])
    message = OutboundMessage(
        kind=MessageKind(p["kind"]),
        text=p.get("text"),
        media=media,
        html=p.get("html", False),
        reply_to=p.get("reply_to"),
    )
    return (
        int(p["chat_id"]), p["dialog_kind"], message,
        p.get("origin_event"), p.get("target_name", ""), p.get("origin_room"),
    )
