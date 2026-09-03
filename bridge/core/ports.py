"""Ports: the abstract boundaries between the core and the outside world.

Adapters implement these Protocols. The core depends only on the Protocols,
never on concrete adapters, which is what keeps the coupling low and lets the
tests substitute in-memory fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

from .models import (
    AccountResult,
    ChatInfo,
    Dialog,
    DialogSummary,
    InboundMessage,
    MediaRef,
    OutboundMessage,
    TelegramTarget,
)

# The vault's account record. Imported lazily by name to keep the core free of
# any dependency on where accounts are stored.
TelegramAccountInfo = Any

# A handler the source calls for every inbound message it observes.
MessageHandler = Callable[[InboundMessage], Awaitable[None]]


@runtime_checkable
class MediaFetcher(Protocol):
    """Fetches the raw bytes behind a `MediaRef`."""

    async def fetch(self, ref: MediaRef) -> bytes: ...


@runtime_checkable
class MessageSource(Protocol):
    """Something that emits inbound messages (the Matrix side)."""

    def set_handler(self, handler: MessageHandler) -> None: ...

    async def start(self) -> None:
        """Run until cancelled, delivering messages to the handler."""

    async def close(self) -> None: ...


@runtime_checkable
class MessageSink(Protocol):
    """Something that delivers outbound messages to a target address.

    The `target.chat_id` is interpreted by the concrete sink: a Telegram chat
    for the Telegram sink, a Matrix room id for the Matrix sink.
    """

    async def deliver(
        self, target: TelegramTarget, message: OutboundMessage
    ) -> Optional[str]:
        """Deliver; return the delivered message's id (as a string) if the
        medium exposes one — a Telegram message id, or a Matrix event id."""

    async def close(self) -> None: ...


@runtime_checkable
class TelegramDirectory(Protocol):
    """Lookup + bulk operations over the Telegram account's conversations."""

    async def list_dialogs(self) -> list[Dialog]: ...

    async def list_dms(self) -> list[DialogSummary]:
        """Private chats only, with unread count and last-message preview."""

    async def resolve(self, query: str, kind: str | None = None) -> Dialog | None:
        """Find a dialog by @username, numeric id, or (partial) name.

        `kind` restricts the search to "user" / "bot" / "group" / "channel", so
        a group sharing a name with a contact cannot win a lookup meant for a
        DM.
        """

    async def presence(self, chat_id: int) -> str:
        """"ok" | "deleted" | "gone" — whether the chat still exists.

        Anything not positively established is "ok": a transient failure must
        not get a live room renamed.
        """

    async def avatar(self, chat_id: int) -> bytes | None:
        """The chat's current profile photo as image bytes, or None if it has
        none (or the account may not see it). Works for users, bots, groups
        and channels alike."""

    async def history(self, query: str, limit: int) -> list[tuple[str, str]]:
        """Return up to `limit` recent (sender, text) pairs, oldest first."""

    async def info(self, query: str) -> ChatInfo | None:
        """Detailed info (about/members/bio/…) for one conversation."""

    async def join(self, query: str) -> Dialog | None:
        """Join a public @username or an invite link; return the joined chat."""

    async def leave(self, chat_id: int) -> None:
        """Leave a group or channel. Raises if Telegram refuses.

        Never used on a private chat: the Telegram call that leaves a DM also
        wipes its history, which is not what "leave" should ever mean.
        """

    async def own_message_stats(self) -> list[tuple[Dialog, int]]:
        """Dialogs where you have messages, with your message count, desc."""

    async def delete_own_messages(self, chat_id: int) -> int:
        """Delete all of your own messages in one chat; return the count."""

    async def delete_message(self, chat_id: int, msg_id: int) -> None:
        """Delete one specific message; raise if not permitted."""


@runtime_checkable
class StickerQuoter(Protocol):
    """Turns owner-typed text into a quote sticker and sends *that* instead.

    The whole round trip (ask the quote bot, wait for its sticker, clean the
    bot chat up, send the sticker on) lives behind this one call, because only
    the Telegram adapter owns the client it needs.
    """

    async def quote_send(
        self, chat_id: int, text: str, reply_to: Optional[int] = None
    ) -> Optional[str]:
        """Return the sticker's Telegram message id, or None if no sticker
        could be made — the caller then sends the text as it was typed, since
        an undelivered message is worse than an unstyled one."""


@runtime_checkable
class RoomCreator(Protocol):
    """Creates a dedicated Matrix room for one Telegram conversation."""

    async def create_chat_room(self, dialog: Dialog) -> str:
        """Create the room (inside the configured space) and return its id.
        Raises on failure — the caller decides the fallback."""

    async def set_topic(self, room_id: str, info: ChatInfo) -> None:
        """Refresh a room's topic from current chat info (best-effort)."""

    async def set_name(self, room_id: str, name: str) -> bool:
        """Rename the room; used to flag a chat that is gone on Telegram."""

    async def set_avatar(self, room_id: str, chat_id: int) -> str:
        """Mirror the Telegram chat's photo onto the room (best-effort).

        Returns why nothing happened, rather than a bare false, so the command
        can tell "they have no photo" from "the upload broke":
          "set"       — the room avatar was changed
          "unchanged" — same photo as the one already mirrored
          "none"      — the chat has no photo this account can see
          "error"     — the fetch or the upload failed (details in the log)
        """


@runtime_checkable
class MatrixRedactor(Protocol):
    """Redacts (deletes) a single Matrix event."""

    async def redact(self, room_id: str, event_id: str) -> None: ...


@runtime_checkable
class MatrixEditor(Protocol):
    """Marks up existing Matrix events instead of destroying them.

    Remote Telegram deletions/edits are reflected here: the Matrix copy is kept
    and annotated so the history stays readable.
    """

    async def replace_event(self, room_id: str, event_id: str, html: str) -> None:
        """Edit an event in place (m.replace)."""

    async def annotate_event(self, room_id: str, event_id: str, html: str) -> None:
        """Post a note anchored to an event (as a reply)."""


@dataclass
class AccountBundle:
    """Everything that belongs to *one* Telegram account.

    The dispatcher holds no per-account collaborator of its own: it resolves a
    bundle first (from the room a message arrived in, or from the current
    account) and then works through it. That is what lets several accounts run
    at once without their rooms, queues or message links ever mixing.
    """

    account: "TelegramAccountInfo"
    directory: TelegramDirectory
    sender: OutboundSender
    registry: Any  # RoomRegistry (core class; typed loosely to stay flat)
    links: Any  # MessageLinks
    reply_map: Any  # ReplyMap — per account, so replies cannot cross accounts
    state: Any  # BridgeState — settings belong to the account, not the bridge
    control_room: str = ""  # where this account is driven from
    rooms: Optional[RoomCreator] = None  # None until a Space is bound


@runtime_checkable
class AccountRouter(Protocol):
    """Directs work to the right Telegram account, and manages the set of them.

    Implemented by the composition root, which is the only place that knows how
    to start and stop a live Telethon client.
    """

    def accounts(self) -> list["TelegramAccountInfo"]:
        """Every SAVED account, most recently used first — online or not.

        Offline accounts stay listed deliberately: a session revoked at
        Telegram still owns a vault entry, a session file and caches, and
        `logout` is the only thing that clears them.
        """

    def is_online(self, tg_id: int) -> bool:
        """Whether this account has a live Telegram client behind it."""

    def current(self) -> Optional[AccountBundle]:
        """The account control-room commands act on. None if none is logged in."""

    def for_room(self, room_id: str) -> Optional[AccountBundle]:
        """The account owning a per-chat room, or None if the room is not ours."""

    def for_control_room(self, room_id: str) -> Optional[AccountBundle]:
        """The account driven from this room, if it is one's control room."""

    def by_query(self, query: str) -> Optional[AccountBundle]:
        """Resolve an account by id, @username, name, or list position."""

    async def set_control_room(self, query: str, room_id: str) -> AccountResult:
        """Make a room an account's control room (or clear it with "")."""

    async def begin_login(
        self, phone: str, space_id: str, control_room: str = ""
    ) -> AccountResult:
        """Ask Telegram to send a login code. Returns stage="code"."""

    async def submit_code(self, code: str) -> AccountResult:
        """Continue a pending login. Returns stage="password" if 2FA is on."""

    async def submit_password(self, password: str) -> AccountResult:
        """Finish a 2FA login."""

    def pending_login(self) -> str:
        """What the pending login is waiting for ("code"/"password"), else ""."""

    def cancel_login(self) -> None: ...

    async def switch_to(self, query: str) -> AccountResult:
        """Make an account current. Non-destructive; every account stays online."""

    async def bind_space(self, query: str, space_id: str) -> AccountResult:
        """Bind (or with an empty space_id, unbind) an account's Matrix Space."""

    async def logout(self, query: str) -> AccountResult:
        """Sign the account out of Telegram, delete its session and caches, and
        drop it from the list. Destructive, unlike a switch."""

    def set_send_failure_handler(self, handler: Optional[Callable]) -> None:
        """Register who is told when an account's *queued* send fails for good.

        Called as `handler(tg_id, chat_id, target_name, origin_room, exc)`.
        Wired by the composition root once the dispatcher exists, and applied
        to accounts that come online later too."""


@runtime_checkable
class SpaceResolver(Protocol):
    """Finds which Matrix Space a room belongs to."""

    async def space_for_room(self, room_id: str) -> Optional[str]:
        """The room's parent Space, or None if it is not inside one."""


@runtime_checkable
class MessageExpirer(Protocol):
    """Schedules self-destruction of a forwarded message (TG + optional Matrix)."""

    async def schedule(
        self,
        chat_id: int,
        msg_id: int,
        delay_seconds: float,
        matrix_room: Optional[str] = None,
        matrix_event: Optional[str] = None,
    ) -> None: ...


@runtime_checkable
class OutboundSender(Protocol):
    """Delivers a message to Telegram, applying send-delay / scheduled-time and
    self-destruct. Returns ("sent", None) if delivered now, or
    ("scheduled", epoch_seconds) if queued for later."""

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
        """`origin_room` is the Matrix room the message was typed in, so
        self-destruct can redact the right room (per-chat rooms differ from
        the control room)."""

    def set_failure_handler(self, handler: Optional[Callable]) -> None:
        """Register who is told when a *deferred* send is given up on.

        Immediate sends report by raising out of `submit`; a queued one fails
        long after the owner stopped looking, so it needs somewhere to go."""
