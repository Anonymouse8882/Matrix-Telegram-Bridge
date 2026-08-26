"""Neutral domain models shared by every layer.

These types are the *lingua franca* of the bridge. Source adapters translate
platform events into an `InboundMessage`; the core turns that into an
`OutboundMessage`; sink adapters translate it back into platform API calls.
Nothing here depends on Matrix or Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class MessageKind(str, Enum):
    """Platform-agnostic classification of a message."""

    TEXT = "text"
    IMAGE = "image"
    STICKER = "sticker"
    VIDEO = "video"
    AUDIO = "audio"
    FILE = "file"


@dataclass(frozen=True)
class MediaRef:
    """A pointer to a media blob living on the source platform.

    The core never resolves this itself; it asks the source adapter to fetch
    the bytes (see `MediaFetcher`). Keeping it a pointer means the domain stays
    ignorant of `mxc://` URLs and other platform details.
    """

    uri: str  # opaque to the core, e.g. an mxc:// URL for Matrix
    mimetype: Optional[str] = None
    filename: Optional[str] = None
    size: Optional[int] = None


@dataclass(frozen=True)
class InboundMessage:
    """A message received from a source, normalised to neutral form."""

    kind: MessageKind
    source_room: str  # the source's address (Matrix room id, or TG chat id)
    sender: str  # human-readable display name of the author
    text: Optional[str] = None  # body text, or caption for media
    media: Optional[MediaRef] = None
    source_label: Optional[str] = None  # e.g. the TG chat/channel name
    # "user" | "bot" | "group" | "channel" (TG side). Bots are split out from
    # users because a bot DM is not a conversation: service and spam bots would
    # otherwise each earn a room under the "DMs always relay" rule.
    source_kind: Optional[str] = None
    source_msg_id: Optional[int] = None  # the Telegram message id (incoming)
    reply_to_event: Optional[str] = None  # Matrix event this replies to (outgoing)
    event_id: Optional[str] = None  # this message's own Matrix event id
    # The Telegram message this one replies to (incoming). Lets the relay hang
    # the Matrix copy off the copy of that message, so a conversation between
    # other people keeps its reply threading on the Matrix side.
    reply_to_msg_id: Optional[int] = None
    # Sent *by* this account rather than received. Relayed too, so a per-chat
    # room shows both halves of the conversation instead of only the replies.
    outgoing: bool = False
    # Who a forwarded message ORIGINALLY came from (None = not a forward).
    # Without it a forward is indistinguishable from something the sender
    # wrote themselves — which misattributes the words to the wrong person.
    forward_from: Optional[str] = None


@dataclass(frozen=True)
class Dialog:
    """A Telegram conversation the account belongs to (user, group, channel)."""

    id: int
    name: str
    kind: str  # "user" | "bot" | "group" | "channel"
    username: Optional[str] = None


@dataclass(frozen=True)
class DialogSummary:
    """A dialog plus its inbox-level state, for listing conversations.

    `last_text` is the raw message text; `last_media` says the last message
    carried media. Rendering the two into something like "[图片]" is the core's
    job, so the adapter stays free of presentation.
    """

    dialog: Dialog
    unread: int = 0
    last_text: str = ""
    last_media: bool = False
    last_outgoing: bool = False  # the last message was sent by you
    last_date: Optional[float] = None  # epoch seconds


@dataclass(frozen=True)
class ChatInfo:
    """Detailed information about a Telegram conversation (for `!tg info` and
    per-chat room topics). Fields absent for a given kind stay None."""

    id: int
    kind: str  # "user" | "bot" | "group" | "channel"
    title: str
    username: Optional[str] = None
    about: Optional[str] = None  # group/channel description, or a user's bio
    members: Optional[int] = None  # group members / channel subscribers
    personal_channel: Optional[str] = None  # a user's linked channel (@name/title)
    is_bot: bool = False
    verified: bool = False


@dataclass(frozen=True)
class OutboundMessage:
    """What the core hands to a sink: the fully-rendered thing to deliver."""

    kind: MessageKind
    text: Optional[str] = None  # rendered caption / body (may contain markup)
    media: Optional[MediaRef] = None
    media_bytes: Optional[bytes] = None
    filename: Optional[str] = None
    mimetype: Optional[str] = None
    silent: bool = False  # deliver without a notification (muted source)
    html: bool = False  # whether `text` is HTML (vs. plain literal text)
    reply_to: Optional[int] = None  # Telegram msg id to reply to (outgoing)
    reply_to_event: Optional[str] = None  # Matrix event to reply to (incoming)


@dataclass(frozen=True)
class AccountResult:
    """The outcome of an account operation (login step, switch, bind, logout).

    Deliberately carries no secret: phone codes and passwords stay inside the
    adapter, so they cannot reach a log line or a room by accident.
    """

    ok: bool
    stage: str = ""  # "code" | "password" | "done" — what the login needs next
    label: str = ""  # human name of the account involved
    detail: str = ""  # extra note worth showing (space bound, caches wiped)
    error: str = ""  # human-readable reason when `ok` is False


@dataclass(frozen=True)
class TelegramTarget:
    """A delivery address for a MessageSink.

    For the Telegram sink this is a chat (numeric id / @username); for the
    Matrix sink `chat_id` carries the Matrix room id. `message_thread_id` maps
    to a Telegram forum topic when present."""

    chat_id: str
    message_thread_id: Optional[int] = None
    reply_to: Optional[int] = None  # Telegram msg id to reply to
    label: str = ""  # human note for logs


# Neutral name for the same address, used where the sink is the *Matrix* one
# and `chat_id` holds a room id — so call sites read as what they mean.
Target = TelegramTarget
