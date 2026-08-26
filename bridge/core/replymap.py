"""Maps a relayed Matrix event back to the Telegram message it came from.

When the owner uses Element's native "reply" on a relayed message, we look the
replied-to Matrix event id up here to send the reply to the right Telegram
chat, threaded under the original message. Kept in memory and bounded — replies
target recent messages, so old entries can be evicted.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ReplyRef:
    chat_id: int
    msg_id: int
    kind: str  # "user" | "bot" | "group" | "channel"
    name: str


class ReplyMap:
    def __init__(self, capacity: int = 5000):
        self._capacity = capacity
        self._map: "OrderedDict[str, ReplyRef]" = OrderedDict()

    def remember(self, event_id: str, ref: ReplyRef) -> None:
        if not event_id:
            return
        self._map[event_id] = ref
        self._map.move_to_end(event_id)
        while len(self._map) > self._capacity:
            self._map.popitem(last=False)

    def lookup(self, event_id: Optional[str]) -> Optional[ReplyRef]:
        if not event_id:
            return None
        return self._map.get(event_id)

    def clear(self) -> None:
        """Forget every mapping (Matrix account change).

        A rebuild of the app would replace this object anyway, but a switch
        that never completes its reload must not leave the previous account's
        event ids paired with Telegram chats in memory.
        """
        self._map.clear()
