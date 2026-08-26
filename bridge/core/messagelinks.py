"""Persistent link between a Telegram message and its Matrix event.

Powers deletion sync in BOTH directions: when a Telegram message is deleted
(by anyone) we redact its Matrix event, and vice versa. Persisted to /data so
it survives restarts — unlike the in-memory ReplyMap, which is also why the
event-id index here is what lets a reply still find its chat after a restart.

The lookup has to cope with a Telegram protocol quirk: `MessageDeleted` events
for DMs and *basic* groups do NOT say which chat the message was in — only the
message id. Telegram message ids in those chats are drawn from one per-account
sequence, so they are unique account-wide and a msg-id-only lookup is safe.
Channel/megagroup deletions DO carry the channel id and have per-channel ids,
so those are matched on (chat, msg).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, fields
from typing import Callable, Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MessageLink:
    chat_id: int
    msg_id: int
    kind: str  # "user" | "bot" | "group" | "channel"
    room_id: str  # Matrix room the event lives in
    event_id: str  # Matrix event to mark / annotate
    ts: float  # when linked (for age-based pruning)
    # Last known Telegram text, and the rendered HTML prefix that preceded it
    # ("<b>[chat]</b> <b>sender</b>", or "" in a DM's own room). Together they
    # let an edit re-render the message exactly as it was first posted.
    text: str = ""
    head: str = ""
    media: bool = False  # media events can't be replaced without losing the file
    # The text as first received. Captured on the FIRST edit (before that,
    # `text` still is the original) and never overwritten, so a message edited
    # repeatedly still shows what it originally said.
    orig: str = ""


_FIELDS = {f.name for f in fields(MessageLink)}


def _is_channel_peer(chat_id: int | str) -> bool:
    """Whether this id is a channel/megagroup peer (the -100… form).

    That is the property the msg-id fallback must exclude, and `kind` is the
    wrong test for it: a megagroup is stored as "group" (it *is* a group to the
    user) but is a channel peer underneath, with its own per-chat message
    counter — so its ids collide with the account-wide sequence that DMs and
    basic groups draw from.
    """
    return str(chat_id).startswith("-100")


class MessageLinks:
    def __init__(
        self,
        path: Optional[str] = None,
        capacity: int = 20000,
        max_age_days: int = 60,
        clock: Callable[[], float] = time.time,
        flush_delay: float = 1.0,
    ):
        self._path = path
        self._capacity = capacity
        self._max_age = max_age_days * 86400
        self._now = clock
        # (chat_id, msg_id) -> link. Insertion order = eviction order.
        self._links: dict[tuple[int, int], MessageLink] = {}
        # Matrix event id -> (chat_id, msg_id). The reverse direction, so a
        # reply naming an event can find the Telegram message it belongs to.
        self._by_event: dict[str, tuple[int, int]] = {}
        # Saves are debounced: this file grows to thousands of records, and
        # rewriting it inline for every relayed message stalls the event loop.
        self._flush_delay = flush_delay
        self._flush_handle: Optional[asyncio.TimerHandle] = None
        self._io_lock = threading.Lock()  # serialises executor writes
        # Set by close(). No write may happen after it: by then the account's
        # whole directory may have been deleted.
        self._closed = False
        self._load()

    def add(
        self, chat_id: int, msg_id: int, kind: str, room_id: str, event_id: str,
        text: str = "", head: str = "", media: bool = False,
    ) -> None:
        if not room_id or not event_id:
            return
        key = (int(chat_id), int(msg_id))
        self._drop(key)  # re-insert at the end (freshest)
        self._links[key] = MessageLink(
            int(chat_id), int(msg_id), kind, room_id, event_id, self._now(),
            text, head, media,
        )
        self._by_event[event_id] = key
        self._evict()
        self._save()

    def update_text(self, chat_id: int, msg_id: int, text: str) -> None:
        """Record a new current text after an edit, keeping the same event.

        The first edit promotes the pre-edit text to `orig`; later edits leave
        it alone, so "original vs current" stays meaningful however many times
        a message is edited.
        """
        key = (int(chat_id), int(msg_id))
        old = self._links.get(key)
        if old is None:
            return
        self._links[key] = MessageLink(
            old.chat_id, old.msg_id, old.kind, old.room_id, old.event_id,
            old.ts, text, old.head, old.media, old.orig or old.text,
        )
        self._save()

    def get(self, chat_id: int, msg_id: int) -> Optional[MessageLink]:
        """Exact (chat, msg) lookup — no cross-chat fallback.

        Used for reply targets, where guessing by message id alone could thread
        a reply under an unrelated message from another chat.
        """
        return self._links.get((int(chat_id), int(msg_id)))

    def by_event(self, event_id: Optional[str]) -> Optional[MessageLink]:
        """The link whose Matrix event this is, or None.

        The reverse of `add`, and the reason it exists: an Element reply names
        an event id, and this index is persisted — so a reply still resolves to
        the right Telegram chat after a restart, where the ReplyMap is empty.
        """
        if not event_id:
            return None
        key = self._by_event.get(event_id)
        return self._links.get(key) if key is not None else None

    def find(self, chat_id: Optional[int], msg_id: int) -> Optional[MessageLink]:
        """Resolve a deleted Telegram message to its Matrix event.

        `chat_id` is None for DM/basic-group deletions (Telegram omits it); we
        then match by msg id among links that are not channel peers, where ids
        are unique.
        """
        if chat_id is not None:
            exact = self._links.get((int(chat_id), int(msg_id)))
            if exact is not None:
                return exact
            if _is_channel_peer(chat_id):
                # A channel/megagroup peer: exact or nothing. Its msg ids are
                # small per-channel counters, so a msg-id-only fallback could
                # hit an unrelated DM link that shares the number — and mark
                # the wrong message as deleted.
                return None
        # No chat named (DM / basic group — Telegram omits the peer there), or
        # a DM/basic-group id in a mismatched form: match by msg id among links
        # that are NOT channel peers, where ids come from one account-wide
        # sequence. Testing the peer form rather than `kind` is what keeps a
        # megagroup (stored as "group") out of this scan.
        for link in reversed(list(self._links.values())):
            if link.msg_id == int(msg_id) and not _is_channel_peer(link.chat_id):
                return link
        return None

    def forget(self, chat_id: int, msg_id: int) -> None:
        self._drop((int(chat_id), int(msg_id)))
        self._save()

    def clear(self) -> None:
        """Drop every link, in memory and on disk (Matrix account change).

        Memory too, not just the file: a live instance would otherwise write
        the old account's message text straight back out on the next `add`.
        """
        self._links.clear()
        self._by_event.clear()
        self._save()

    def close(self) -> None:
        """Write out any debounced change and disarm the timer.

        Both halves are about the timer outliving its owner. Without the flush
        a restart loses up to `flush_delay` of links, and those messages
        silently stop syncing deletions and edits. Without the cancel, a timer
        firing after the account's directory was deleted (`!tg logout`)
        re-creates it — `_write_records` makes the directory — resurrecting the
        message text the user just asked to erase.
        """
        if self._closed:
            return
        self._closed = True
        handle, self._flush_handle = self._flush_handle, None
        if handle is None:
            return  # nothing pending: the file already matches memory
        handle.cancel()
        self._write_records([asdict(v) for v in self._links.values()])

    # -- housekeeping --------------------------------------------------------

    def _drop(self, key: tuple[int, int]) -> None:
        """Remove a link and its event-id index entry together."""
        link = self._links.pop(key, None)
        if link is not None and self._by_event.get(link.event_id) == key:
            del self._by_event[link.event_id]

    def _evict(self) -> None:
        cutoff = self._now() - self._max_age
        # Age-prune from the front (oldest); dict preserves insertion order.
        for key, link in list(self._links.items()):
            if link.ts < cutoff:
                self._drop(key)
            else:
                break
        while len(self._links) > self._capacity:
            self._drop(next(iter(self._links)))

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001 - a bad file must not stop the bridge
            log.exception("failed to load message links from %s", self._path)
            return
        if not isinstance(data, list):
            log.warning("ignoring %s: not a list of links", self._path)
            return
        skipped = 0
        for rec in data:
            # Per record, not per file. One unreadable entry — a field a newer
            # version added, a half-written record — must cost that entry
            # alone; failing the whole file would silently disable deletion and
            # edit sync for every message the bridge has ever relayed.
            try:
                link = MessageLink(**{k: v for k, v in rec.items() if k in _FIELDS})
            except Exception:  # noqa: BLE001
                skipped += 1
                continue
            key = (link.chat_id, link.msg_id)
            self._links[key] = link
            self._by_event[link.event_id] = key
        if skipped:
            log.warning("skipped %d unreadable message link(s) in %s",
                        skipped, self._path)
        self._evict()

    def _save(self) -> None:
        """Persist, debounced: many changes within `flush_delay` become one
        write, and the write itself runs in a worker thread. Without a running
        loop (unit tests, shutdown) it degrades to an immediate write."""
        if not self._path or self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_records([asdict(v) for v in self._links.values()])
            return
        if self._flush_handle is not None:
            return  # a flush is already scheduled; it will see this change too
        self._flush_handle = loop.call_later(self._flush_delay, self._flush, loop)

    def _flush(self, loop: asyncio.AbstractEventLoop) -> None:
        self._flush_handle = None
        if self._closed:
            return
        records = [asdict(v) for v in self._links.values()]  # snapshot now
        loop.run_in_executor(None, self._write_records, records)

    def _write_records(self, records: list[dict]) -> None:
        try:
            with self._io_lock:
                os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
                tmp = f"{self._path}.tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(records, fh, ensure_ascii=False)
                os.replace(tmp, self._path)
        except Exception:  # noqa: BLE001
            log.exception("failed to save message links to %s", self._path)
