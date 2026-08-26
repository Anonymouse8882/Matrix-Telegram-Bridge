"""Room creation adapter: one private Matrix room per Telegram conversation.

Implements the `RoomCreator` port on the SAME nio client the source owns.
Each room is created inside the configured Space (both directions of the
space relationship are written, so Element renders the hierarchy properly).

No invites are ever sent: the bridge *is* the owner's account, so a room it
creates is already the owner's room.
"""

from __future__ import annotations

import hashlib
import io
import logging

from nio import AsyncClient, RoomCreateResponse

from ..core.models import ChatInfo, Dialog
from ..core.transformer import format_topic

log = logging.getLogger(__name__)

_KIND_ICON = {
    "user": "\U0001f464",     # 👤
    "bot": "\U0001f916",      # 🤖
    "group": "\U0001f465",    # 👥
    "channel": "\U0001f4e2",  # 📢
}


class MatrixRooms:
    def __init__(
        self,
        client: AsyncClient,
        space_id: str,
        homeserver_name: str,
        directory=None,
    ):
        self._client = client
        self._space = space_id
        # The `via` server for space-child pointers, e.g. "matrix.org".
        self._via = homeserver_name
        self._dir = directory  # TelegramDirectory, for topics and avatars
        # chat id -> hash of the photo last mirrored, so refreshing a room
        # (`!tg info`, `!tg avatar all`) does not re-upload an unchanged image
        # or spam the room with avatar-change events.
        self._avatar_hashes: dict[int, str] = {}

    async def _topic_for(self, dialog: Dialog) -> str:
        # Best-effort: a failed info lookup must not block room creation.
        if self._dir is not None:
            try:
                info = await self._dir.info(str(dialog.id))
                if info is not None:
                    return format_topic(info)
            except Exception:  # noqa: BLE001
                log.exception("topic lookup failed for %s", dialog.id)
        bits = [f"Telegram {dialog.kind} {dialog.id}"]
        if dialog.username:
            bits.append(f"@{dialog.username}")
        return " · ".join(bits)

    async def create_chat_room(self, dialog: Dialog) -> str:
        name = f"{_KIND_ICON.get(dialog.kind, '')} {dialog.name}".strip()
        topic = await self._topic_for(dialog)

        initial_state = []
        if self._space:
            # Child -> parent pointer, canonical so clients group it correctly.
            initial_state.append({
                "type": "m.space.parent",
                "state_key": self._space,
                "content": {"via": [self._via], "canonical": True},
            })

        resp = await self._client.room_create(
            name=name,
            topic=topic,
            initial_state=initial_state,
        )
        if not isinstance(resp, RoomCreateResponse):
            raise RuntimeError(f"room_create failed for {dialog.name}: {resp}")
        room_id = resp.room_id

        if self._space:
            # Parent -> child pointer. Failure here is cosmetic (the room works,
            # it just doesn't appear inside the space), so log loudly but keep
            # the room rather than failing the whole relay.
            child = await self._client.room_put_state(
                self._space,
                "m.space.child",
                {"via": [self._via], "suggested": False},
                state_key=room_id,
            )
            if getattr(child, "event_id", None) is None:
                log.warning("could not add %s to space %s: %s",
                            room_id, self._space, child)

        # Cosmetic, and it costs a download plus an upload — so it must never
        # be able to fail a room that already exists and works.
        await self.set_avatar(room_id, dialog.id)

        log.info("created room %s for %s (%s)", room_id, dialog.name, dialog.id)
        return room_id

    async def set_name(self, room_id: str, name: str) -> bool:
        """Rename a room (used to flag a chat that no longer exists)."""
        try:
            await self._client.room_put_state(
                room_id, "m.room.name", {"name": name},
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to rename %s", room_id)
            return False
        log.info("renamed %s to %r", room_id, name)
        return True

    async def set_topic(self, room_id: str, info: ChatInfo) -> None:
        """Refresh a room's topic (e.g. on `!tg info`). Best-effort."""
        try:
            await self._client.room_put_state(
                room_id, "m.room.topic", {"topic": format_topic(info)},
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to set topic for %s", room_id)

    async def set_avatar(self, room_id: str, chat_id: int) -> str:
        """Mirror the Telegram chat's photo onto the Matrix room.

        Returns "set" / "unchanged" / "none" / "error" — see the `RoomCreator`
        port. Callers report the reason, so a missing avatar is diagnosable
        without reading the container log.
        """
        if self._dir is None:
            return "error"
        try:
            data = await self._dir.avatar(int(chat_id))
        except Exception:  # noqa: BLE001 - purely decorative
            log.warning("avatar lookup failed for %s", chat_id, exc_info=True)
            return "error"
        if not data:
            return "none"

        digest = hashlib.sha256(data).hexdigest()
        if self._avatar_hashes.get(int(chat_id)) == digest:
            return "unchanged"

        try:
            resp, _ = await self._client.upload(
                lambda *_: io.BytesIO(data),
                content_type="image/jpeg",  # what Telegram hands out
                filename="avatar.jpg",
                filesize=len(data),
            )
            uri = getattr(resp, "content_uri", None)
            if not uri:
                log.warning("avatar upload failed for %s: %s", room_id, resp)
                return "error"
            await self._client.room_put_state(
                room_id, "m.room.avatar", {"url": uri},
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to set avatar for %s", room_id)
            return "error"
        self._avatar_hashes[int(chat_id)] = digest
        log.info("avatar synced for %s -> %s", chat_id, room_id)
        return "set"
