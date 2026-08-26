"""Telegram user-account source + directory adapter (Telethon).

Shares one Telethon client with the sink. Provides three ports at once because
they all need that same authorised client:

  * MessageSource   — listens to every incoming message across all dialogs
  * MediaFetcher    — downloads media for an incoming message
  * TelegramDirectory — list / resolve / read-history over the account's chats
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable, Optional

from telethon import TelegramClient, events, utils
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.channels import (
    GetFullChannelRequest,
    JoinChannelRequest,
    LeaveChannelRequest,
)
from telethon.tl.functions.messages import (
    DeleteChatUserRequest,
    GetFullChatRequest,
    ImportChatInviteRequest,
)
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    Channel,
    ChannelForbidden,
    Chat,
    ChatForbidden,
    InputUserSelf,
    PeerChannel,
    User,
)

from ..core.models import (
    ChatInfo,
    Dialog,
    DialogSummary,
    InboundMessage,
    MediaRef,
    MessageKind,
)
from ..core.ports import MessageHandler
from ..core.transformer import UNKNOWN_ORIGIN

log = logging.getLogger(__name__)

# Handler for deletions: (chat_peer_id_or_None, deleted_msg_ids).
DeleteHandler = "Callable[[Optional[int], list[int]], Awaitable[None]]"

_INVITE_RE = re.compile(r"(?:t\.me/|telegram\.me/)(?:joinchat/|\+)([\w-]+)")
_USERNAME_RE = re.compile(r"(?:t\.me/|telegram\.me/|@)?([A-Za-z0-9_]{3,})/?$")


class TelegramUserSource:
    def __init__(self, client: TelegramClient):
        self._client = client
        self._handler: Optional[MessageHandler] = None
        self._delete_handler = None  # DeleteHandler
        self._edit_handler = None
        # Telethon dispatches every update as its own task, so two messages
        # from one chat are processed concurrently — and a message that has to
        # download media lands in Matrix *after* a later plain-text one. One
        # queue per chat restores the order Telegram sent them in, while
        # different chats still make progress independently.
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: list[asyncio.Task] = []

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def set_delete_handler(self, handler) -> None:
        """handler(chat_peer_id | None, [msg_id, ...]) on remote deletions."""
        self._delete_handler = handler

    def set_edit_handler(self, handler) -> None:
        """handler(chat_peer_id, msg_id, new_text) on remote edits."""
        self._edit_handler = handler

    async def start(self) -> None:
        # Both directions: incoming, and what this account sends from another
        # device (marked `outgoing`), so a room shows the whole conversation.
        # The relay's echo guard drops the copies of the bridge's own sends —
        # those are already in Matrix, typed there by the operator.
        self._client.add_event_handler(self._on_new_message, events.NewMessage())
        self._client.add_event_handler(self._on_deleted, events.MessageDeleted())
        self._client.add_event_handler(self._on_edited, events.MessageEdited())
        log.info("telegram source listening for incoming messages")
        await self._client.run_until_disconnected()

    # -- per-chat ordering ---------------------------------------------------

    def _enqueue(self, key, work: Callable[[], Awaitable[None]]) -> None:
        """Run `work` on this chat's queue, after everything already on it."""
        room = str(key)
        queue = self._queues.get(room)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[room] = queue
            self._workers.append(asyncio.create_task(self._drain(queue)))
        queue.put_nowait(work)

    async def _drain(self, queue: asyncio.Queue) -> None:
        while True:
            work = await queue.get()
            try:
                await work()
            except Exception:  # noqa: BLE001 - one bad update mustn't kill the chat
                log.exception("telegram update handler failed")
            finally:
                queue.task_done()

    async def _on_deleted(self, event: events.MessageDeleted.Event) -> None:
        if self._delete_handler is None:
            return
        # `chat_id` is the -100… peer id for channels/megagroups and None for
        # DMs and basic groups (Telegram's delete update omits the peer there)
        # — the core then resolves by account-unique message id. Queued on the
        # chat it names so a deletion cannot overtake the message it refers to.
        chat_peer = event.chat_id
        ids = list(event.deleted_ids)
        handler = self._delete_handler
        self._enqueue(chat_peer, lambda: handler(chat_peer, ids))

    async def _on_edited(self, event) -> None:
        if self._edit_handler is None:
            return
        handler = self._edit_handler
        chat_id, msg_id = event.chat_id, event.id
        text = event.message.message or ""
        self._enqueue(chat_id, lambda: handler(chat_id, msg_id, text))

    async def close(self) -> None:
        for worker in self._workers:
            worker.cancel()
        self._workers.clear()
        self._queues.clear()
        if self._client.is_connected():
            await self._client.disconnect()

    # -- incoming messages ---------------------------------------------------

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        if self._handler is None:
            return
        # Queue rather than handle inline: normalising and relaying both await
        # (chat lookup, media download), and Telethon would otherwise let a
        # later message finish first. See `_enqueue`.
        self._enqueue(event.chat_id, lambda: self._relay(event))

    async def _relay(self, event: events.NewMessage.Event) -> None:
        try:
            msg = await self._to_inbound(event)
        except Exception:  # noqa: BLE001 - one bad event mustn't kill the loop
            log.exception("failed to normalise telegram event")
            return
        if self._handler is not None:
            await self._handler(msg)

    async def _to_inbound(self, event: events.NewMessage.Event) -> InboundMessage:
        m = event.message
        chat = await event.get_chat()
        label = utils.get_display_name(chat) or str(event.chat_id)
        sender = await event.get_sender()
        sender_name = (
            utils.get_display_name(sender)
            or getattr(m, "post_author", None)
            or label
        )
        kind = _kind_of(m)
        if event.is_private:
            # `chat` is the peer in both directions (for an outgoing message
            # the *sender* is us, so it cannot answer "is this a bot").
            source_kind = "bot" if getattr(chat, "bot", False) else "user"
        elif event.is_channel and not event.is_group:
            source_kind = "channel"
        else:
            source_kind = "group"
        text = m.message or None
        if kind is MessageKind.STICKER and not text:
            # The sticker's own emoji, so the relayed line says *which* sticker
            # it was. Stickers carry no caption, so this cannot overwrite one.
            text = getattr(getattr(m, "file", None), "emoji", None) or None

        media = None
        if kind is not MessageKind.TEXT:
            f = getattr(m, "file", None)
            media = MediaRef(
                uri=f"{event.chat_id}:{m.id}",  # how fetch() re-locates it
                mimetype=getattr(f, "mime_type", None),
                filename=getattr(f, "name", None),
                size=getattr(f, "size", None),
            )
        return InboundMessage(
            kind=kind,
            source_room=str(event.chat_id),
            sender=sender_name,
            text=text,
            media=media,
            source_label=label,
            source_kind=source_kind,
            source_msg_id=m.id,
            reply_to_msg_id=_reply_to_msg_id(m),
            outgoing=bool(getattr(m, "out", False)),
            forward_from=await _forward_origin(m),
        )

    # -- MediaFetcher port ---------------------------------------------------

    async def fetch(self, ref: MediaRef) -> bytes:
        chat_s, _, msg_s = ref.uri.partition(":")
        message = await self._client.get_messages(int(chat_s), ids=int(msg_s))
        if message is None:
            raise RuntimeError(f"telegram message gone: {ref.uri}")
        data = await self._client.download_media(message, file=bytes)
        if not isinstance(data, (bytes, bytearray)):
            raise RuntimeError("telegram media download did not return bytes")
        return bytes(data)

    # -- TelegramDirectory port ---------------------------------------------

    async def presence(self, chat_id: int) -> str:
        """Whether the Telegram side of a chat still exists.

          "ok"      — resolvable and alive
          "deleted" — the account was deleted (Telegram keeps the peer, empty)
          "gone"    — cannot be resolved, or we were thrown out of it

        Deliberately conservative: anything we cannot positively establish is
        reported as "ok", because renaming a live room to "已删除" over a
        transient lookup failure would be worse than not noticing.
        """
        try:
            ent = await self._client.get_entity(int(chat_id))
        except (ValueError, TypeError):
            # Telethon raises these when the peer cannot be resolved at all,
            # which is what a deleted chat/channel looks like from here.
            log.info("presence: %s can no longer be resolved", chat_id)
            return "gone"
        except Exception:  # noqa: BLE001 - network / flood: not evidence
            log.warning("presence: lookup failed for %s", chat_id, exc_info=True)
            return "ok"
        if getattr(ent, "deleted", False):
            return "deleted"
        if isinstance(ent, (ChannelForbidden, ChatForbidden)):
            return "gone"
        return "ok"

    async def avatar(self, chat_id: int) -> Optional[bytes]:
        """The chat's profile photo (big variant) as JPEG bytes, or None.

        Two attempts, because the short entity is not always enough: Telethon
        reads `entity.photo`, which is empty for a "min" user (one we only know
        from a message, not from a dialog fetch) even when the account can see
        the photo perfectly well. The full-user record then still has it.
        """
        try:
            data = await self._client.download_profile_photo(
                int(chat_id), file=bytes
            )
        except Exception:  # noqa: BLE001 - cosmetic; never break the caller
            log.warning("avatar: download failed for %s", chat_id, exc_info=True)
            return None
        if isinstance(data, (bytes, bytearray)) and data:
            return bytes(data)
        if int(chat_id) > 0:  # a user or bot: try the full record
            return await self._full_user_photo(int(chat_id))
        log.info("avatar: %s has no photo we can see", chat_id)
        return None

    async def _full_user_photo(self, user_id: int) -> Optional[bytes]:
        """A user's photo from `GetFullUser`, for when the short entity's is
        empty. Covers min users and photos only exposed on the full record."""
        try:
            full = await self._client(GetFullUserRequest(user_id))
        except Exception:  # noqa: BLE001
            log.warning("avatar: full-user lookup failed for %s", user_id,
                        exc_info=True)
            return None
        fu = full.full_user
        photo = (
            getattr(fu, "profile_photo", None)
            or getattr(fu, "personal_photo", None)
            or getattr(fu, "fallback_photo", None)
        )
        if photo is None:
            log.info("avatar: user %s has no photo we can see", user_id)
            return None
        try:
            data = await self._client.download_media(photo, file=bytes)
        except Exception:  # noqa: BLE001
            log.warning("avatar: photo download failed for %s", user_id,
                        exc_info=True)
            return None
        if isinstance(data, (bytes, bytearray)) and data:
            log.info("avatar: recovered %s's photo from the full record", user_id)
            return bytes(data)
        return None

    async def list_dialogs(self) -> list[Dialog]:
        return [_dialog_of(d) async for d in self._client.iter_dialogs()]

    async def resolve(self, query: str, kind: Optional[str] = None) -> Optional[Dialog]:
        q = query.strip()
        bare = q.lstrip("@")  # "@name" and "name" are the same lookup

        # @username or numeric id -> ask Telegram directly.
        if q.startswith("@") or _is_int(bare):
            try:
                ent = await self._client.get_entity(int(bare) if _is_int(bare) else q)
                dialog = _entity_to_dialog(ent)
                if kind is None or dialog.kind == kind:
                    return dialog
            except Exception:  # noqa: BLE001
                pass  # fall through: the name/username scan below may still hit

        # Scan the dialog list. Usernames are matched here too - get_entity only
        # resolves usernames Telegram will hand out, and a plain `@name` typed
        # as a send target must still work for anyone already in the list.
        lowered = bare.lower()
        if not lowered:
            return None
        exact = None
        partial = None
        async for d in self._client.iter_dialogs():
            if kind is not None and _kind_of_dialog(d) != kind:
                continue
            name = (d.name or "").lower()
            uname = (getattr(d.entity, "username", None) or "").lower()
            if lowered in (name, uname):
                exact = d
                break
            if partial is None and (lowered in name or lowered in uname):
                partial = d
        chosen = exact or partial
        return _dialog_of(chosen) if chosen is not None else None

    async def list_dms(self) -> list[DialogSummary]:
        out: list[DialogSummary] = []
        async for d in self._client.iter_dialogs():
            # Real people only: a bot chat is not correspondence, and mixing
            # them in buried actual DMs under service notifications.
            if _kind_of_dialog(d) != "user":
                continue
            msg = getattr(d, "message", None)
            date = getattr(msg, "date", None) if msg else None
            out.append(
                DialogSummary(
                    dialog=_dialog_of(d),
                    unread=getattr(d, "unread_count", 0) or 0,
                    last_text=(getattr(msg, "message", "") or "") if msg else "",
                    last_media=bool(getattr(msg, "media", None)) if msg else False,
                    last_outgoing=bool(getattr(msg, "out", False)) if msg else False,
                    last_date=date.timestamp() if date is not None else None,
                )
            )
        # Unread first, then most recently active - an inbox, not an address book.
        out.sort(key=lambda s: (0 if s.unread else 1, -(s.last_date or 0)))
        return out

    async def own_message_stats(self) -> list[tuple[Dialog, int]]:
        out: list[tuple[Dialog, int]] = []
        async for d in self._client.iter_dialogs():
            try:
                msgs = await self._client.get_messages(
                    d.entity, from_user="me", limit=0
                )
                total = getattr(msgs, "total", 0) or 0
            except Exception:  # noqa: BLE001 - some chats disallow the query
                total = 0
            if total <= 0:
                continue
            out.append((_dialog_of(d), total))
        out.sort(key=lambda t: -t[1])
        return out

    async def info(self, query: str) -> Optional[ChatInfo]:
        q = query.strip()
        try:
            ent = await self._client.get_entity(int(q) if _is_int(q) else q)
        except Exception:  # noqa: BLE001
            log.exception("info: get_entity failed for %r", query)
            return None
        did = utils.get_peer_id(ent)
        title = utils.get_display_name(ent) or str(did)
        username = getattr(ent, "username", None)
        verified = bool(getattr(ent, "verified", False))

        try:
            if isinstance(ent, User):
                return await self._user_info(ent, did, title, username, verified)
            if isinstance(ent, Channel):
                return await self._channel_info(ent, did, title, username, verified)
            if isinstance(ent, Chat):
                return await self._basic_group_info(ent, did, title)
        except Exception:  # noqa: BLE001 - full-info calls can fail (privacy/flood)
            log.exception("info: full lookup failed for %s", did)
        # Fall back to what the plain entity already gave us.
        kind = _entity_to_dialog(ent).kind
        return ChatInfo(id=did, kind=kind, title=title, username=username,
                        verified=verified, is_bot=bool(getattr(ent, "bot", False)))

    async def _user_info(self, ent, did, title, username, verified) -> ChatInfo:
        full = await self._client(GetFullUserRequest(ent))
        fu = full.full_user
        personal = None
        pc_id = getattr(fu, "personal_channel_id", None)
        if pc_id:
            for c in full.chats:
                if c.id == pc_id:
                    personal = f"@{c.username}" if getattr(c, "username", None) else c.title
                    break
        is_bot = bool(getattr(ent, "bot", False))
        return ChatInfo(
            id=did, kind="bot" if is_bot else "user", title=title,
            username=username,
            about=getattr(fu, "about", None), personal_channel=personal,
            is_bot=is_bot, verified=verified,
        )

    async def _channel_info(self, ent, did, title, username, verified) -> ChatInfo:
        full = await self._client(GetFullChannelRequest(ent))
        fc = full.full_chat
        # A megagroup is a "group" to us; a broadcast channel is a "channel".
        kind = "group" if getattr(ent, "megagroup", False) else "channel"
        return ChatInfo(
            id=did, kind=kind, title=title, username=username,
            about=getattr(fc, "about", None),
            members=getattr(fc, "participants_count", None),
            verified=verified,
        )

    async def _basic_group_info(self, ent, did, title) -> ChatInfo:
        full = await self._client(GetFullChatRequest(ent.id))
        fc = full.full_chat
        members = getattr(ent, "participants_count", None)
        parts = getattr(fc, "participants", None)
        if members is None and parts is not None:
            members = len(getattr(parts, "participants", []) or [])
        return ChatInfo(id=did, kind="group", title=title,
                        about=getattr(fc, "about", None), members=members)

    async def join(self, query: str) -> Optional[Dialog]:
        q = query.strip()
        invite = _INVITE_RE.search(q)
        if invite or q.startswith("+"):
            invite_hash = invite.group(1) if invite else q[1:]
            try:
                updates = await self._client(ImportChatInviteRequest(invite_hash))
                chat = updates.chats[0]
            except UserAlreadyParticipantError:
                return await self.resolve(q)  # already in; report it anyway
            except Exception:  # noqa: BLE001
                log.exception("join via invite failed for %r", query)
                return None
            return _entity_to_dialog(chat)

        m = _USERNAME_RE.search(q)
        target = m.group(1) if m else q
        try:
            ent = await self._client.get_entity(target)
            await self._client(JoinChannelRequest(ent))
        except UserAlreadyParticipantError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("join via username failed for %r", query)
            return None
        return _entity_to_dialog(ent)

    async def leave(self, chat_id: int) -> None:
        """Leave a group or channel.

        Two different Telegram calls hide behind one word: channels and
        megagroups are *left*, while a basic group is left by deleting yourself
        from its member list. Telethon's `delete_dialog` would paper over the
        difference but also wipes history for a private chat, so the peer type
        is resolved explicitly instead.
        """
        entity = await self._client.get_entity(int(chat_id))
        if isinstance(entity, User):
            raise ValueError("cannot leave a private chat")
        if isinstance(entity, Channel):
            await self._client(LeaveChannelRequest(entity))
            return
        await self._client(DeleteChatUserRequest(entity.id, InputUserSelf()))

    async def delete_message(self, chat_id: int, msg_id: int) -> None:
        # revoke=True deletes for everyone; raises if we lack permission
        # (e.g. deleting others' messages in a group/channel without rights).
        await self._client.delete_messages(chat_id, [msg_id], revoke=True)

    async def delete_own_messages(self, chat_id: int) -> int:
        deleted = 0
        batch: list[int] = []
        async for m in self._client.iter_messages(chat_id, from_user="me"):
            batch.append(m.id)
            if len(batch) >= 100:
                await self._client.delete_messages(chat_id, batch, revoke=True)
                deleted += len(batch)
                batch = []
        if batch:
            await self._client.delete_messages(chat_id, batch, revoke=True)
            deleted += len(batch)
        return deleted

    async def history(self, query: str, limit: int) -> list[tuple[str, str]]:
        ent = await self._client.get_entity(int(query) if _is_int(query) else query)
        messages = await self._client.get_messages(ent, limit=limit)
        rows: list[tuple[str, str]] = []
        for m in reversed(messages):  # oldest first
            sender = await m.get_sender()
            name = (
                utils.get_display_name(sender)
                or getattr(m, "post_author", None)
                or "?"
            )
            body = m.message or ("[" + _kind_of(m).value + "]" if m.media else "")
            rows.append((name, body))
        return rows


def _kind_of_dialog(d) -> str:
    """Classify a Telethon dialog the way the whole bridge names kinds."""
    if d.is_user:
        return "bot" if getattr(d.entity, "bot", False) else "user"
    return "group" if d.is_group else "channel"


def _dialog_of(d) -> Dialog:
    """A neutral `Dialog` from a Telethon dialog object."""
    return Dialog(
        id=d.id,
        name=d.name or str(d.id),
        kind=_kind_of_dialog(d),
        username=getattr(d.entity, "username", None),
    )


def _reply_to_msg_id(m) -> Optional[int]:
    """The message this one actually replies to, or None.

    In a forum, *every* message carries a `reply_to` pointing at its topic
    root; only when `reply_to_top_id` is set is the message a genuine reply to
    another message (the top id then holds the topic). Treating the topic root
    as a reply target would thread whole topics under their first post.
    """
    header = getattr(m, "reply_to", None)
    if header is None:
        return None
    if getattr(header, "forum_topic", False) and getattr(header, "reply_to_top_id", None) is None:
        return None
    msg_id = getattr(header, "reply_to_msg_id", None)
    return int(msg_id) if msg_id else None


async def _forward_origin(m) -> Optional[str]:
    """Who a forwarded message originally came from, or None if not a forward.

    Telethon splits the origin two ways: a forwarded *person* lands in the
    header's sender, a forwarded *channel post* in its chat. Only one of the
    two is ever populated, so the sender is tried first and the chat only when
    there is none — which also keeps the lookup to a single call.
    """
    header = getattr(m, "fwd_from", None)
    if header is None:
        return None  # not a forward at all
    origin = getattr(m, "forward", None)
    sender = await _origin_entity(origin, "get_sender")
    chat = await _origin_entity(origin, "get_chat") if sender is None else None
    return _forward_label(
        utils.get_display_name(sender) if sender is not None else None,
        utils.get_display_name(chat) if chat is not None else None,
        getattr(header, "from_name", None),
        getattr(header, "post_author", None),
    )


async def _origin_entity(origin, method: str):
    """Resolve one side of a forward header, or None.

    A forward can point at an account this one cannot see (privacy settings,
    a deleted user, a channel we never joined). That must cost the message its
    attribution at worst, never the message itself.
    """
    resolve = getattr(origin, method, None)
    if resolve is None:
        return None
    try:
        return await resolve()
    except Exception:  # noqa: BLE001 - unresolvable origin; fall back to the name
        log.debug("forward origin: %s failed", method, exc_info=True)
        return None


def _forward_label(
    sender_name: Optional[str],
    chat_name: Optional[str],
    from_name: Optional[str],
    post_author: Optional[str],
) -> str:
    """Render the origin of a forward from the parts Telegram supplies.

    `from_name` is the bare string left when the original account forbids
    being linked — Telegram itself shows exactly that, so it is a real
    attribution and not a fallback for failure. `post_author` is a channel
    post's signature, added alongside the channel it appeared in.
    """
    who = (sender_name or "").strip() or (from_name or "").strip()
    where = (chat_name or "").strip()
    author = (post_author or "").strip()
    if who and where and who != where:
        return f"{where} · {who}"
    name = who or where
    if author and author != name:
        return f"{name} · {author}" if name else author
    return name or UNKNOWN_ORIGIN


def _kind_of(m) -> MessageKind:
    if getattr(m, "sticker", None):
        return MessageKind.STICKER
    if getattr(m, "photo", None):
        return MessageKind.IMAGE
    if getattr(m, "video", None) or getattr(m, "video_note", None) or getattr(m, "gif", None):
        return MessageKind.VIDEO
    if getattr(m, "voice", None) or getattr(m, "audio", None):
        return MessageKind.AUDIO
    if getattr(m, "document", None):
        return MessageKind.FILE
    return MessageKind.TEXT


def _entity_to_dialog(ent) -> Dialog:
    did = utils.get_peer_id(ent)
    name = utils.get_display_name(ent) or str(did)
    if isinstance(ent, User):
        kind = "bot" if getattr(ent, "bot", False) else "user"
    elif isinstance(ent, Channel):
        kind = "group" if getattr(ent, "megagroup", False) else "channel"
    elif isinstance(ent, Chat):
        kind = "group"
    else:
        kind = "channel"
    return Dialog(id=did, name=name, kind=kind, username=getattr(ent, "username", None))


def _is_int(s: str) -> bool:
    s = s.strip()
    if s.startswith("-"):
        s = s[1:]
    return s.isdigit()
