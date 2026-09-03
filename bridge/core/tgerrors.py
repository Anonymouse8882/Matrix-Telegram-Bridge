"""Name the cause of a failed Telegram send.

A send can fail because the network hiccuped — or because the chat will never
accept another message, which is a completely different thing to tell the owner
and the only case where renaming their Matrix room is justified. Telegram says
which it is, in the RPC error it returns; this module turns that into a word
the core can act on.

Errors are matched by class *name*, walking the exception's MRO, so the core
keeps no import of Telethon: the classification stays testable with a plain
stand-in class, and a Bot-API sink could report the same vocabulary.
"""

from __future__ import annotations

# The person on the other end deleted their Telegram account. Unambiguous:
# Telegram itself says the target user is gone, so no second opinion is needed.
_DEACTIVATED = frozenset({
    "InputUserDeactivatedError",  # "The specified user was deleted"
    "UserDeletedError",           # same, reported from a secret-chat send
})

# The peer cannot be addressed any more (chat deleted, channel we were thrown
# out of, id no longer valid). Weaker evidence than the above — a stale access
# hash looks exactly like this — so callers confirm before acting on it.
_UNREACHABLE = frozenset({
    "PeerIdInvalidError",
    "ChannelPrivateError",
    "ChatIdInvalidError",
    "UserIdInvalidError",
    "UserInvalidError",
})

# *Our* account is the dead one. Kept separate on purpose: reporting this as
# "they deleted their account" — or worse, renaming their room over it — would
# blame the wrong side for an outage that needs a re-login to fix.
_SELF_GONE = frozenset({
    "UserDeactivatedError",
    "UserDeactivatedBanError",
    "AuthKeyUnregisteredError",
    "SessionRevokedError",
    "SessionExpiredError",
})


def failure_reason(exc: BaseException) -> str:
    """Why a send failed: "deleted" | "gone" | "self" | "".

    "" means no permanent cause could be named, and the caller must treat the
    failure as transient — dropping a queued message or renaming a live room
    over a network blip is worse than retrying one that was never coming back.
    """
    for cls in type(exc).__mro__:
        name = cls.__name__
        if name in _DEACTIVATED:
            return "deleted"
        if name in _SELF_GONE:
            return "self"
        if name in _UNREACHABLE:
            return "gone"
    return ""


def is_permanent(exc: BaseException) -> bool:
    """Whether retrying this send could ever succeed.

    A queued message for a peer Telegram refuses to address is not waiting on
    the network; it is waiting forever. "self" is excluded — a re-login fixes
    that one, and the queue should still be there when it happens.
    """
    return failure_reason(exc) in ("deleted", "gone")
