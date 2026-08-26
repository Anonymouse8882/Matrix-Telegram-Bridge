"""Mutable bridge state: the active outgoing target per room, and which
Telegram sources are muted (delivered without a notification).

Kept out of the adapters so the control logic can be tested without any I/O.
Optionally persisted to a small JSON file so choices survive a restart.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


class BridgeState:
    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._active: dict[str, str] = {}  # matrix room -> target query
        self._muted: set[str] = set()  # muted Telegram chat ids (as str)
        self._watched: set[str] = set()  # allowlisted group/channel ids to relay
        # seconds after which forwarded messages self-destruct, per target kind
        self._self_destruct: dict[str, int] = {
            "user": 0, "bot": 0, "group": 0, "channel": 0,
        }
        # global send delay: every forward waits fixed + uniform(0, random) secs
        self._delay_fixed: int = 0
        self._delay_random: int = 0
        self._command_prefix: str = ""  # empty = use the configured default
        self._load()

    # -- command prefix (overrides the configured default when set) ----------

    def command_prefix(self) -> str:
        return self._command_prefix

    def set_command_prefix(self, prefix: str) -> None:
        self._command_prefix = prefix.strip()
        self._save()

    # -- active outgoing target ---------------------------------------------

    def active_target(self, room: str) -> Optional[str]:
        return self._active.get(room)

    def set_active_target(self, room: str, target: str) -> None:
        self._active[room] = target
        self._save()

    # -- mute state ----------------------------------------------------------

    def is_muted(self, chat_id: str) -> bool:
        return str(chat_id) in self._muted

    def mute(self, chat_id: str) -> None:
        self._muted.add(str(chat_id))
        self._save()

    def unmute(self, chat_id: str) -> None:
        self._muted.discard(str(chat_id))
        self._save()

    def muted(self) -> set[str]:
        return set(self._muted)

    # -- relay allowlist (groups/channels; DMs always relay) -----------------

    def is_watched(self, chat_id: str) -> bool:
        return str(chat_id) in self._watched

    def watch(self, chat_id: str) -> None:
        self._watched.add(str(chat_id))
        self._save()

    def unwatch(self, chat_id: str) -> None:
        self._watched.discard(str(chat_id))
        self._save()

    def watched(self) -> set[str]:
        return set(self._watched)

    # -- self-destruct TTL (seconds) per target kind -------------------------

    def self_destruct(self, kind: str) -> int:
        return int(self._self_destruct.get(kind, 0))

    def set_self_destruct(self, kind: str, seconds: int) -> None:
        self._self_destruct[kind] = int(seconds)
        self._save()

    def self_destruct_all(self) -> dict[str, int]:
        return dict(self._self_destruct)

    # -- send delay ----------------------------------------------------------

    def delay_fixed(self) -> int:
        return self._delay_fixed

    def delay_random(self) -> int:
        return self._delay_random

    def set_delay(self, fixed: int, random_max: int) -> None:
        self._delay_fixed = max(0, int(fixed))
        self._delay_random = max(0, int(random_max))
        self._save()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._active = dict(data.get("active", {}))
            self._muted = set(data.get("muted", []))
            self._watched = set(data.get("watched", []))
            self._self_destruct.update(data.get("self_destruct", {}))
            self._delay_fixed = int(data.get("delay_fixed", 0))
            self._delay_random = int(data.get("delay_random", 0))
            self._command_prefix = str(data.get("command_prefix", ""))
        except Exception:  # noqa: BLE001 - corrupt state shouldn't crash startup
            log.exception("failed to load state from %s", self._path)

    def _save(self) -> None:
        if not self._path:
            return
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            # Temp file then rename, as every other cache here does. Writing in
            # place truncates first, so a process killed mid-write leaves
            # half a JSON document — which `_load` turns into "no settings at
            # all", silently discarding the watch list, mutes and TTLs.
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "active": self._active,
                        "muted": sorted(self._muted),
                        "watched": sorted(self._watched),
                        "self_destruct": self._self_destruct,
                        "delay_fixed": self._delay_fixed,
                        "delay_random": self._delay_random,
                        "command_prefix": self._command_prefix,
                    },
                    fh,
                    ensure_ascii=False,
                    indent=2,
                )
            os.replace(tmp, self._path)
        except Exception:  # noqa: BLE001
            log.exception("failed to save state to %s", self._path)
