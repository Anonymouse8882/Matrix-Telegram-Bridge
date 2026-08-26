"""Self-destruct: delete forwarded Telegram messages after their TTL.

Pending deletions are persisted to a small JSON file so they survive restarts
(TTLs can be days). A background sweeper deletes due messages; anything already
overdue at startup is deleted on the first pass.

The Matrix copy is *marked*, not destroyed — the same treatment a message
deleted by someone else gets, and for the same reason: the bridge exists to
keep a readable record. A self-destructing message vanishing from Telegram
while its Matrix copy silently disappeared too would leave no trace that
anything was ever said.
"""

from __future__ import annotations

import asyncio
import logging
import time

from telethon import TelegramClient

from .jsonfile import load_json_list, save_json_list

log = logging.getLogger(__name__)


class TelegramExpirer:
    def __init__(
        self,
        client: TelegramClient,
        path: str,
        deletion_marker=None,  # Callable[[int | None, list[int]], Awaitable]
        interval: float = 15.0,
    ):
        self._client = client
        self._path = path
        # Same handler remote deletions go through, so a self-destructed
        # message and one deleted by its author look identical in Matrix.
        self._marker = deletion_marker
        self._interval = interval
        self._pending: list[dict] = load_json_list(path)
        self._lock = asyncio.Lock()

    def set_marker(self, marker) -> None:
        """Wired after construction: the marker lives in the relay, which is
        built later because it needs this expirer's sibling components."""
        self._marker = marker

    async def schedule(
        self,
        chat_id: int,
        msg_id: int,
        delay_seconds: float,
        matrix_room=None,
        matrix_event=None,
    ) -> None:
        async with self._lock:
            self._pending.append({
                "chat": int(chat_id),
                "msg": int(msg_id),
                "at": time.time() + float(delay_seconds),
                "mx_room": matrix_room,
                "mx_event": matrix_event,
            })
            self._save()

    async def run(self) -> None:
        log.info("self-destruct sweeper running (%d pending)", len(self._pending))
        while True:
            await self._sweep()
            await asyncio.sleep(self._interval)

    async def _sweep(self) -> None:
        now = time.time()
        async with self._lock:
            due = [p for p in self._pending if p["at"] <= now]
        for p in due:
            try:
                await self._client.delete_messages(
                    p["chat"], [p["msg"]], revoke=True
                )
            except Exception:  # noqa: BLE001 - already gone / no perms: drop it
                log.warning("self-destruct(tg) failed for %s/%s", p["chat"], p["msg"])
            # Mark the Matrix copy as deleted (struck through), never redact it.
            if self._marker is not None:
                try:
                    await self._marker(p["chat"], [p["msg"]])
                except Exception:  # noqa: BLE001
                    log.warning("self-destruct(matrix) failed for %s/%s",
                                p["chat"], p["msg"])
        if due:
            done = {id(p) for p in due}
            async with self._lock:
                self._pending = [p for p in self._pending if id(p) not in done]
                self._save()

    # -- persistence ---------------------------------------------------------

    def _save(self) -> None:
        save_json_list(self._path, self._pending, label="expirer state")
