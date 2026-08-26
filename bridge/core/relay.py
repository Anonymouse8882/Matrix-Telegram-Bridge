"""Incoming relay: Telegram -> Matrix.

Every message the account receives (from any group / channel / DM) that passes
the relay filter is posted into Matrix. With a Space configured, each
conversation gets its own room (created lazily on first message); without one,
or when creation fails, messages land in the global control room — degraded
readability, never a dropped message.

Pure orchestration over ports; no SDK imports.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Awaitable, Callable, Optional

from .messagelinks import MessageLinks
from .models import InboundMessage, MessageKind, OutboundMessage, Target
from .ports import MatrixEditor, MediaFetcher, MessageSink, RoomCreator
from .render import panel
from .replymap import ReplyMap, ReplyRef
from .roomregistry import RoomRegistry
from .state import BridgeState
from .transformer import (
    escape_html,
    incoming_head,
    join_head_body,
    with_forward,
)

log = logging.getLogger(__name__)

# A Telegram sticker relays as this line instead of the image: sticker files are
# .webp/.tgs, which most Matrix clients render as a broken image or not at all.
STICKER_LABEL = "【sticker】"

# How the account's own messages are attributed in a room where every Matrix
# event has the same sender.
SELF_LABEL = "我"


class Relay:
    def __init__(
        self,
        matrix_sink: MessageSink,
        telegram_fetcher: MediaFetcher,
        state: BridgeState,
        control_room: str,
        reply_map: ReplyMap | None = None,
        registry: RoomRegistry | None = None,
        rooms: RoomCreator | None = None,
        on_new_room: Optional[Callable[[str], None]] = None,
        links: MessageLinks | None = None,
        editor: MatrixEditor | None = None,
        pending_delete_ttl: float = 900.0,
        clock: Callable[[], float] = time.time,
    ):
        self._mx = matrix_sink
        self._fetcher = telegram_fetcher
        self._state = state
        self._room = control_room
        self._reply_map = reply_map
        self._registry = registry
        self._rooms = rooms  # None = per-chat rooms disabled (no space set)
        self._on_new_room = on_new_room  # tells the source to watch a new room
        self._links = links  # tg<->matrix message map for deletion/edit sync
        self._editor = editor
        # Deletions that arrived before the message they refer to was linked —
        # see `_remember_pending_delete`. (chat_id | None, msg_id) -> when.
        self._pending_deletes: "OrderedDict[tuple, float]" = OrderedDict()
        self._pending_ttl = pending_delete_ttl
        self._now = clock
        # Telegram updates are dispatched concurrently, so the first two
        # messages of a new chat can race into room creation; one lock per chat
        # makes the loser reuse the winner's room instead of creating a second.
        self._create_locks: dict[str, asyncio.Lock] = {}

    def set_control_room(self, room: str) -> None:
        """Re-point the fallback room (the account's control room moved)."""
        self._room = room

    def set_rooms(self, registry: RoomRegistry | None, rooms: RoomCreator | None) -> None:
        """Re-point at a different Space (an account was bound or unbound).

        Swapped in place rather than rebuilt so the account's Telegram client,
        and everything already in flight on it, keeps running.
        """
        self._registry = registry
        self._rooms = rooms

    def _should_relay(self, msg: InboundMessage) -> bool:
        # A real person's DM relays by default — that is the conversation the
        # bridge exists for.
        if msg.source_kind == "user":
            return True
        if msg.source_kind == "bot":
            # Bots are opt-in by the allow-list ALONE, deliberately not by
            # owning a room: service and spam bots (Telegram's own 777000,
            # "Spam Info Bot", …) DM you unprompted, and under the old
            # DM-always-relays rule each one earned a room nobody asked for.
            # Ignoring an existing room here also silences the ones already
            # created, without having to delete them.
            return self._state.is_watched(msg.source_room)
        # A dedicated room IS the opt-in for groups/channels: creating one (by
        # hand or because the chat was watched earlier) says "I want to read
        # this here", and a room that silently receives nothing is confusing.
        if self._registry is not None and self._registry.room_for(msg.source_room):
            return True
        return self._state.is_watched(msg.source_room)

    async def _room_for(self, msg: InboundMessage) -> tuple[str, bool]:
        """(room to deliver into, is_dedicated). Falls back to the control room.

        Creation failures (rate limits, transient errors) must degrade the
        layout, not lose the message — so any error lands in the control room
        and the next message simply tries again.
        """
        if self._registry is None or self._rooms is None:
            return self._room, False
        existing = self._registry.room_for(msg.source_room)
        if existing:
            await self._clear_deleted_mark(msg, existing)
            return existing, True
        lock = self._create_locks.setdefault(msg.source_room, asyncio.Lock())
        async with lock:
            # A concurrent message may have created the room while we waited.
            existing = self._registry.room_for(msg.source_room)
            if existing:
                return existing, True
            from .models import Dialog  # local import keeps module deps flat

            dialog = Dialog(
                id=int(msg.source_room),
                name=msg.source_label or msg.source_room,
                kind=msg.source_kind or "user",
            )
            try:
                room_id = await self._rooms.create_chat_room(dialog)
            except Exception:  # noqa: BLE001
                log.exception("room creation failed for %s; using control room",
                              dialog.name)
                return self._room, False
            self._registry.register(
                msg.source_room, room_id, dialog.name, kind=dialog.kind
            )
        if self._on_new_room is not None:
            self._on_new_room(room_id)
        return room_id, True

    async def _clear_deleted_mark(self, msg: InboundMessage, room: str) -> None:
        """A chat that just sent a message is plainly not deleted.

        `!tg check` can only guess from a lookup, and Telegram sometimes hides
        a peer that later comes back; a live message is the one unambiguous
        proof, so the room name is restored automatically.
        """
        if not self._registry.is_deleted(msg.source_room):
            return
        if not self._registry.set_deleted(msg.source_room, False):
            return
        name = msg.source_label or self._registry.name_for(msg.source_room)
        if self._rooms is not None and name:
            try:
                await self._rooms.set_name(room, name)
                log.info("chat %s is alive again; restored room name",
                         msg.source_room)
            except Exception:  # noqa: BLE001 - cosmetic
                log.debug("could not restore the name of %s", room)

    def _head(self, msg: InboundMessage, dedicated: bool) -> str:
        """The HTML prefix for a relayed message; stored so edits can reuse it."""
        # Every relayed event has the bridge account as its Matrix sender, so
        # without a marker your own messages would be indistinguishable from
        # the other side's — especially in a DM's room, which shows no name.
        who = SELF_LABEL if msg.outgoing else msg.sender
        # A forward is someone else's words: the marker rides along on every
        # head shape, so the origin is never lost to the layout.
        return with_forward(self._base_head(msg, dedicated, who), msg.forward_from)

    def _base_head(self, msg: InboundMessage, dedicated: bool, who: str) -> str:
        if not dedicated:
            return incoming_head(msg.source_label or msg.source_room, who)
        # Inside a dedicated room the chat label is redundant. The sender still
        # matters in groups/channels; in a one-to-one chat only "was this me".
        if msg.source_kind in ("user", "bot") and not msg.outgoing:
            return ""
        return f"<b>{escape_html(who)}</b>"

    def _is_own_echo(self, msg: InboundMessage) -> bool:
        """Whether this is Telegram echoing back something the bridge just sent.

        Such a message is already in Matrix — the operator typed it there — so
        relaying the echo would post every outgoing message twice. The link the
        scheduler writes on send is what identifies it, and it is written
        before this handler can run.
        """
        if not msg.outgoing or self._links is None or msg.source_msg_id is None:
            return False
        try:
            chat_id = int(msg.source_room)
        except (TypeError, ValueError):
            return False
        return self._links.get(chat_id, msg.source_msg_id) is not None

    def _reply_target(self, msg: InboundMessage, room: str) -> Optional[str]:
        """The Matrix event to hang this message off, if it replies to one.

        A Telegram reply is only reproducible when we already relayed the
        message it points at *into this same room* — an event id from another
        room would be a dangling relation Element cannot render.
        """
        if self._links is None or msg.reply_to_msg_id is None:
            return None
        try:
            chat_id = int(msg.source_room)
        except (TypeError, ValueError):
            return None
        link = self._links.get(chat_id, msg.reply_to_msg_id)
        if link is None:
            # Sent before the bridge saw the chat, or already pruned. The
            # message still relays; it just loses the visual threading.
            log.debug("reply target not linked: chat %s msg %s",
                      chat_id, msg.reply_to_msg_id)
            return None
        return link.event_id if link.room_id == room else None

    async def on_telegram_message(self, msg: InboundMessage) -> None:
        if not self._should_relay(msg):
            return  # filtered out before any media download
        if self._is_own_echo(msg):
            return  # the bridge sent it; Matrix already has the original
        label = msg.source_label or msg.source_room
        silent = self._state.is_muted(msg.source_room)
        room, dedicated = await self._room_for(msg)
        head = self._head(msg, dedicated)
        sticker = msg.kind is MessageKind.STICKER
        # A sticker becomes a line of text: its emoji (carried in `text` by the
        # source) says which one it was, which is all the file would have shown
        # anyway in a client that cannot decode .webp/.tgs.
        body = f"{STICKER_LABEL}{msg.text or ''}" if sticker else (msg.text or "")
        caption = join_head_body(head, body)

        media_bytes = None
        if not sticker and msg.kind is not MessageKind.TEXT and msg.media is not None:
            try:
                media_bytes = await self._fetcher.fetch(msg.media)
            except Exception:  # noqa: BLE001 - drop the blob, still show the line
                log.exception("failed to fetch telegram media %s", msg.media.uri)

        kind = msg.kind if media_bytes else MessageKind.TEXT
        outbound = OutboundMessage(
            kind=kind,
            text=caption,
            media_bytes=media_bytes,
            filename=msg.media.filename if msg.media else None,
            mimetype=msg.media.mimetype if msg.media else None,
            silent=silent,
            html=True,
            reply_to_event=self._reply_target(msg, room),
        )
        try:
            event_id = await self._mx.deliver(Target(chat_id=room), outbound)
        except Exception:  # noqa: BLE001
            log.exception("failed to post into matrix room")
            return
        # Remember which TG message this Matrix event maps to, so an Element
        # reply to it can be threaded back to the right Telegram message.
        if self._reply_map and event_id and msg.source_msg_id is not None:
            self._reply_map.remember(
                event_id,
                ReplyRef(
                    chat_id=int(msg.source_room),
                    msg_id=msg.source_msg_id,
                    kind=msg.source_kind or "user",
                    name=label,
                ),
            )
        # Persist the tg<->matrix link so a later deletion (either side) can
        # find and mark the counterpart, even across a restart.
        if self._links and event_id and msg.source_msg_id is not None:
            self._links.add(
                int(msg.source_room), msg.source_msg_id,
                msg.source_kind or "user", room, event_id, body,
                head=head, media=media_bytes is not None,
            )
            # Spam is often deleted within the same second it is posted, so a
            # delete update can overtake this link. If one already did, apply
            # it now rather than lose it.
            if self._take_pending_delete(int(msg.source_room), msg.source_msg_id):
                log.info("applying delete that arrived before the link: %s/%s",
                         msg.source_room, msg.source_msg_id)
                await self.on_telegram_deleted(
                    int(msg.source_room), [msg.source_msg_id]
                )

    async def on_telegram_deleted(
        self, chat_id: Optional[int], msg_ids: list[int]
    ) -> None:
        """A Telegram message was deleted remotely -> MARK its Matrix copy.

        Deliberately not a redaction: the point of the bridge is to keep a
        readable record, so the text stays and is struck through instead.
        """
        if self._links is None or self._editor is None:
            return
        for msg_id in msg_ids:
            link = self._links.find(chat_id, msg_id)
            if link is None:
                self._on_unlinked_delete(chat_id, msg_id)
                continue
            try:
                if link.text and not link.media:
                    body = (f"🗑️ <del>{escape_html(link.text)}</del> "
                            f"<i>（已被删除）</i>")
                    await self._editor.replace_event(
                        link.room_id, link.event_id,
                        f"{link.head}: {body}" if link.head else body,
                    )
                else:
                    # Media (or captionless): replacing would destroy the file,
                    # so anchor a note to it instead.
                    await self._editor.annotate_event(
                        link.room_id, link.event_id,
                        panel("🗑️ 已删除", ["对方删除了这条消息（内容保留在上方）"]),
                    )
                log.info("marked deleted: chat %s msg %s -> %s",
                         link.chat_id, link.msg_id, link.event_id)
            except Exception:  # noqa: BLE001 - gone, redacted, or no permission
                log.debug("could not mark %s in %s as deleted",
                          link.event_id, link.room_id)
            # Terminal state; forgetting also makes duplicate delete updates
            # (Telegram sends them) a no-op.
            self._links.forget(link.chat_id, link.msg_id)

    # -- deletions that overtake their own message ---------------------------

    def _on_unlinked_delete(self, chat_id: Optional[int], msg_id: int) -> None:
        """A delete update we cannot act on yet — decide whether to keep it.

        Two very different cases hide behind "no link". A chat we do not relay
        produces these constantly (every deletion in every group the account is
        in), and they are pure noise. A chat we *do* relay means either an old
        message from before the bridge saw it, or — the interesting one — a
        message deleted so fast that the delete overtook the relay. Keeping a
        tombstone costs nothing and rescues that case.
        """
        if not self._relays_chat(chat_id):
            log.debug("delete ignored: chat %s is not relayed (msg %s)",
                      chat_id, msg_id)
            return
        self._remember_pending_delete(chat_id, msg_id)
        log.info("delete pending: no link yet for chat %s msg %s "
                 "(will apply if it arrives)", chat_id, msg_id)

    def _relays_chat(self, chat_id: Optional[int]) -> bool:
        """Whether messages from this chat reach Matrix at all.

        `None` means Telegram omitted the peer (DM / basic group), and a user's
        DM always relays — so an unknown chat has to count as relayed. A
        positive id is a one-to-one chat whose bot-ness we cannot tell from the
        id alone; counting it in only keeps a tombstone, which is harmless.
        """
        if chat_id is None or chat_id > 0:  # unknown, or a one-to-one chat
            return True
        room = str(chat_id)
        if self._state.is_watched(room):
            return True
        return bool(self._registry and self._registry.room_for(room))

    def _remember_pending_delete(self, chat_id: Optional[int], msg_id: int) -> None:
        self._expire_pending()
        self._pending_deletes[(chat_id, int(msg_id))] = self._now()
        while len(self._pending_deletes) > 2000:
            self._pending_deletes.popitem(last=False)

    def _take_pending_delete(self, chat_id: int, msg_id: int) -> bool:
        """Pop a tombstone for this message, matching how `find` resolves ids."""
        self._expire_pending()
        # The delete update may have named the chat or not; both refer here.
        for key in ((int(chat_id), int(msg_id)), (None, int(msg_id))):
            if key in self._pending_deletes:
                del self._pending_deletes[key]
                return True
        return False

    def _expire_pending(self) -> None:
        """Drop tombstones whose message never arrived (it never will)."""
        cutoff = self._now() - self._pending_ttl
        for key, when in list(self._pending_deletes.items()):
            if when < cutoff:
                del self._pending_deletes[key]
            else:
                break  # insertion order is chronological

    async def on_telegram_edited(
        self, chat_id: Optional[int], msg_id: int, new_text: str
    ) -> None:
        """A Telegram message was edited -> edit the Matrix message in place.

        Uses a native Matrix edit (m.replace), so Element shows the message as
        "(edited)" and keeps every previous version in its edit history — the
        original stays readable without cluttering the room with extra events.
        """
        if self._links is None or self._editor is None:
            return
        link = self._links.find(chat_id, msg_id)
        if link is None:
            # Usually a message relayed before this feature existed, or one the
            # relay filter dropped. Logged so "it didn't sync" is diagnosable.
            log.info("edit ignored: no link for chat %s msg %s", chat_id, msg_id)
            return
        if new_text == (link.text or ""):
            # Telegram also "edits" a message when it attaches a link preview;
            # without a text change there is nothing worth showing.
            log.debug("edit ignored: text unchanged for msg %s", msg_id)
            return
        original = link.orig or link.text or ""
        try:
            if link.media:
                # Replacing a media event with text would drop the file, so a
                # caption edit is reported alongside it instead.
                await self._editor.annotate_event(
                    link.room_id, link.event_id,
                    panel("✏️ 已编辑", [f"原：{original or '（空）'}",
                                        f"新：{new_text or '（空）'}"]),
                )
            else:
                # Edit in place, but keep the original visible: Element's edit
                # history is a click away, and the whole point of the bridge is
                # that you can see what was changed without digging.
                body = (
                    f"{escape_html(new_text)}<br/>"
                    f"<i>✏️ 原：<del>{escape_html(original)}</del></i>"
                )
                await self._editor.replace_event(
                    link.room_id, link.event_id,
                    f"{link.head}: {body}" if link.head else body,
                )
            log.info("marked edited: chat %s msg %s -> %s",
                     link.chat_id, link.msg_id, link.event_id)
        except Exception:  # noqa: BLE001
            log.debug("could not apply edit to %s", link.event_id)
        self._links.update_text(link.chat_id, link.msg_id, new_text)
