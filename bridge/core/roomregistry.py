"""Persistent chat_id <-> Matrix room mapping for per-chat rooms.

One Telegram conversation gets (at most) one dedicated Matrix room. The
registry is the single source of truth for that pairing, used by:

  * Relay      — route an incoming TG message to its room (or trigger creation)
  * Dispatcher — route a message typed in a per-chat room back to its TG chat

Persisted as JSON on /data so mappings survive restarts; losing this file is
not fatal (rooms would be re-created and the old ones orphaned) but avoiding
that is exactly why it is persisted.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


class RoomRegistry:
    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._by_chat: dict[str, str] = {}  # tg chat id (str) -> matrix room id
        self._by_room: dict[str, str] = {}  # matrix room id -> tg chat id (str)
        self._names: dict[str, str] = {}  # tg chat id (str) -> display name
        # tg chat id (str) -> "user" | "bot" | "group" | "channel". Cached so routing a
        # typed message back to its chat needs no Telegram lookup per message.
        self._kinds: dict[str, str] = {}
        # Chats whose Telegram side is gone -> why ("deleted" = the account was
        # deactivated, "gone" = the chat no longer exists). Their room is
        # renamed to say so; the reason is kept because the two read
        # differently to a person, and because a chat can move from one to the
        # other. Kept here so the rename happens once, and can be undone if the
        # chat turns out to be alive after all.
        self._deleted: dict[str, str] = {}
        self._load()

    def room_for(self, chat_id: int | str) -> Optional[str]:
        return self._by_chat.get(str(chat_id))

    def chat_for(self, room_id: str) -> Optional[int]:
        chat = self._by_room.get(room_id)
        return int(chat) if chat is not None else None

    def name_for(self, chat_id: int | str) -> str:
        return self._names.get(str(chat_id), "")

    def kind_for(self, chat_id: int | str) -> str:
        """The chat's cached kind, or "" for mappings from before it was kept."""
        return self._kinds.get(str(chat_id), "")

    def is_deleted(self, chat_id: int | str) -> bool:
        """Whether this chat is currently marked as gone on the Telegram side."""
        return str(chat_id) in self._deleted

    def deleted_reason(self, chat_id: int | str) -> str:
        """"deleted" | "gone" | "" — why the chat is marked, if it is."""
        return self._deleted.get(str(chat_id), "")

    def set_deleted(
        self, chat_id: int | str, deleted: bool = True, reason: str = "gone"
    ) -> bool:
        """Record (or clear) the deleted mark. Returns True if it changed, so
        the caller only renames the Matrix room when something actually did.

        A mark that is already there but for a *different* reason counts as a
        change: a chat first seen as unreachable and later confirmed
        deactivated should end up with the room name that says so.
        """
        key = str(chat_id)
        if not deleted:
            if key not in self._deleted:
                return False
            del self._deleted[key]
        else:
            if self._deleted.get(key) == reason:
                return False
            self._deleted[key] = reason
        self._save()
        return True

    def register(
        self, chat_id: int | str, room_id: str, name: str = "", kind: str = ""
    ) -> None:
        key = str(chat_id)
        # A remap must not leave the old room claiming the chat.
        old_room = self._by_chat.get(key)
        if old_room and old_room != room_id:
            self._by_room.pop(old_room, None)
        self._by_chat[key] = room_id
        self._by_room[room_id] = key
        if name:
            self._names[key] = name
        if kind:
            self._kinds[key] = kind
        self._save()

    def clear(self) -> None:
        """Forget every mapping (Matrix account change).

        The rooms themselves belong to the old account and are left orphaned
        there; the new account starts with a clean space.
        """
        self._by_chat.clear()
        self._by_room.clear()
        self._names.clear()
        self._kinds.clear()
        self._deleted.clear()
        self._save()

    def rooms(self) -> set[str]:
        """Every mapped Matrix room id (for the source's watch list)."""
        return set(self._by_room)

    def items(self) -> list[tuple[int, str, str]]:
        """(chat_id, room_id, name) for every mapping, for `!tg rooms`."""
        return [
            (int(chat), room, self._names.get(chat, ""))
            for chat, room in self._by_chat.items()
        ]

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._by_chat = {str(k): str(v) for k, v in data.get("rooms", {}).items()}
            self._by_room = {v: k for k, v in self._by_chat.items()}
            self._names = {str(k): str(v) for k, v in data.get("names", {}).items()}
            self._kinds = {str(k): str(v) for k, v in data.get("kinds", {}).items()}
            self._deleted = _deleted_marks(data.get("deleted"))
        except Exception:  # noqa: BLE001 - a bad file must not stop the bridge
            log.exception("failed to load room registry from %s", self._path)

    def _save(self) -> None:
        if not self._path:
            return
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {"rooms": self._by_chat, "names": self._names,
                     "kinds": self._kinds,
                     "deleted": dict(sorted(self._deleted.items()))},
                    fh, ensure_ascii=False, indent=2,
                )
            os.replace(tmp, self._path)
        except Exception:  # noqa: BLE001
            log.exception("failed to save room registry to %s", self._path)


def _deleted_marks(raw) -> dict[str, str]:
    """Read the deleted marks, tolerating the pre-reason format.

    Registries written before the reason was recorded hold a plain list of chat
    ids. Those chats really are marked, and their rooms really are renamed, so
    they are kept — as the weaker "gone", which is all the old file claimed.
    """
    if isinstance(raw, dict):
        return {str(k): str(v) or "gone" for k, v in raw.items()}
    if isinstance(raw, list):
        return {str(k): "gone" for k in raw}
    return {}
