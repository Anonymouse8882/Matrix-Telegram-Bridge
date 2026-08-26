"""Finding which Matrix Space a room belongs to.

Used to bind a Telegram account to a Space by simply running the command in a
room inside it — no room ids to copy out of Element.

Matrix records the relationship twice and neither side is guaranteed: the room
may carry `m.space.parent`, and the space may carry `m.space.child`. Element
writes the child pointer reliably and the parent one only sometimes, so we
check the room first (one request) and fall back to scanning the joined spaces
(one request each, but only when the cheap answer was missing).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from nio import AsyncClient

log = logging.getLogger(__name__)

# How many rooms to probe at once. Enough to make a few hundred rooms bearable,
# small enough not to look like a burst to the homeserver.
_SCAN_CONCURRENCY = 8


class MatrixSpaces:
    def __init__(self, client: AsyncClient):
        self._client = client

    async def space_for_room(self, room_id: str) -> Optional[str]:
        parent = await self._declared_parent(room_id)
        if parent:
            return parent
        return await self._scan_spaces(room_id)

    async def is_space(self, room_id: str) -> bool:
        """Whether a room is itself a Space (so it can be named directly)."""
        create = await self._state_event(room_id, "m.room.create", "")
        return bool(create) and create.get("type") == "m.space"

    # -- internals -----------------------------------------------------------

    async def _declared_parent(self, room_id: str) -> Optional[str]:
        """The room's own `m.space.parent`, preferring the canonical one."""
        try:
            resp = await self._client.room_get_state(room_id)
        except Exception:  # noqa: BLE001
            log.debug("could not read state of %s", room_id)
            return None
        events = getattr(resp, "events", None)
        if not events:
            return None
        fallback = None
        for event in events:
            if event.get("type") != "m.space.parent":
                continue
            state_key = event.get("state_key")
            if not state_key or not event.get("content", {}).get("via"):
                continue  # an emptied pointer: the relationship was removed
            if event["content"].get("canonical"):
                return state_key
            fallback = fallback or state_key
        return fallback

    async def _scan_spaces(self, room_id: str) -> Optional[str]:
        """Ask each joined space whether it claims this room as a child.

        Probed in small concurrent batches rather than one room at a time: the
        bridge creates a Matrix room per Telegram chat, so a mature install has
        hundreds of joined rooms and a strictly serial scan makes `bind` and
        `login` sit through hundreds of round-trips. Batching keeps the answer
        deterministic (the first claimant in joined-room order still wins)
        while bounding how much is in flight at the homeserver.
        """
        candidates = [r for r in await self._joined_rooms() if r != room_id]
        for start in range(0, len(candidates), _SCAN_CONCURRENCY):
            batch = candidates[start:start + _SCAN_CONCURRENCY]
            claims = await asyncio.gather(
                *(self._claims_child(c, room_id) for c in batch)
            )
            for candidate, claimed in zip(batch, claims, strict=True):
                if claimed:
                    return candidate
        return None

    async def _claims_child(self, candidate: str, room_id: str) -> bool:
        """Whether `candidate` is a Space listing `room_id` as its child."""
        if not await self.is_space(candidate):
            return False
        child = await self._state_event(candidate, "m.space.child", room_id)
        return bool(child and child.get("via"))

    async def _joined_rooms(self) -> list[str]:
        """Every joined room, from the server. The client's in-memory room map
        only holds rooms seen in *this* run's sync responses, which since the
        bridge resumes from a saved sync token is no longer all of them."""
        try:
            resp = await self._client.joined_rooms()
            rooms = getattr(resp, "rooms", None)
            if rooms:
                return list(rooms)
        except Exception:  # noqa: BLE001
            log.debug("joined_rooms failed; falling back to the local room map")
        return list(getattr(self._client, "rooms", {}))

    async def _state_event(self, room_id: str, kind: str, key: str) -> Optional[dict]:
        try:
            resp = await self._client.room_get_state_event(room_id, kind, key)
        except Exception:  # noqa: BLE001
            return None
        content = getattr(resp, "content", None)
        if not isinstance(content, dict) or "errcode" in content:
            return None
        return content
