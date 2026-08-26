"""Destroy Matrix-side caches when the bridge changes account.

Switching account must not hand the next account the previous one's data. Three
kinds of leftovers matter:

  * **content** — `msglinks.json` stores the text of relayed messages;
  * **pointers** — `rooms.json`, `outbox.json` and `state.json`'s active-target
    map name rooms that belong to the old account;
  * **session** — the nio store holds its sync position and device keys.

What is *not* purged is anything about Telegram: the Telegram session is
untouched by a Matrix account change, so mutes, the watch list, send delay and
self-destruct TTLs survive. Pending self-destructs survive too, with their
Matrix references stripped — dropping them would silently cancel deletions the
user is relying on, which is a worse failure than a stale room id.

One module so the CLI (`bridge.mxlogin`) and the in-Matrix `!tg login` command
cannot drift apart: a purge that differs between the two paths is a leak.

Every one of these caches lives *per Telegram account*, under
`accounts/tg-<id>/` — so the sweep has to visit each account directory, not
just the data root. The root is still swept because installs predating
multi-account kept the same files there.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from typing import Optional

log = logging.getLogger(__name__)

# Caches destroyed wholesale: every one of them is either relayed content or a
# pointer into the old account's rooms.
DISCARDED_FILES = ("msglinks.json", "rooms.json", "outbox.json")

# Atomic writers (creds, links, registry) write `<name>.tmp` and then rename.
# A process killed between the two leaves the temp file behind holding a full
# copy - message text, or an access token - so deleting only the real file
# would leave the whole point of the purge sitting next to it.
LEFTOVER_TEMPS = DISCARDED_FILES + ("matrix_creds.json", "state.json", "expire.json")


def purge_matrix_data(data_dir: str, store_path: str = "") -> list[str]:
    """Wipe the old account's Matrix caches. Returns what was actually removed.

    Best-effort by design: a failure to delete one cache must not abort an
    account switch that has already happened, so problems are logged and the
    remaining caches are still cleared.
    """
    removed: list[str] = []
    for label, directory in _cache_dirs(data_dir):
        removed.extend(_purge_dir(directory, label))
    if store_path and _clear_dir(store_path):
        removed.append("matrix store")
    return removed


def _cache_dirs(data_dir: str) -> list[tuple[str, str]]:
    """Every directory that can hold these caches, as (label, path).

    Each Telegram account owns a copy of the whole set under
    `accounts/tg-<id>/` (see `AccountRuntime`), which is where they actually
    live today; the data root only still holds them for installs from before
    accounts owned their own directory. Both are swept, so neither layout can
    hand the incoming Matrix account the previous one's data.
    """
    dirs = [("", data_dir)]
    parent = os.path.join(data_dir, "accounts")
    if not os.path.isdir(parent):
        return dirs
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        log.exception("could not list %s during account purge", parent)
        return dirs
    for name in names:
        path = os.path.join(parent, name)
        # `.pending` is a half-finished *Telegram* login. A Matrix account
        # change has no business touching it.
        if name.startswith(".") or not os.path.isdir(path):
            continue
        dirs.append((f"{name}/", path))
    return dirs


def _purge_dir(directory: str, label: str) -> list[str]:
    """Clear one directory's caches; `label` prefixes what is reported back."""
    removed: list[str] = []
    for name in DISCARDED_FILES:
        if _remove(os.path.join(directory, name)):
            removed.append(f"{label}{name}")
    for name in LEFTOVER_TEMPS:
        if _remove(os.path.join(directory, f"{name}.tmp")):
            removed.append(f"{label}{name}.tmp (残留临时文件)")
    if _strip_matrix_refs(os.path.join(directory, "expire.json")):
        removed.append(f"{label}expire.json (仅清除 Matrix 引用)")
    if _strip_active_targets(os.path.join(directory, "state.json")):
        removed.append(f"{label}state.json (仅清除房间→目标映射)")
    return removed


def _remove(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        log.exception("could not remove %s during account purge", path)
        return False


def _clear_dir(path: str) -> bool:
    """Empty a directory without removing it (nio expects it to exist)."""
    if not os.path.isdir(path):
        return False
    cleared = False
    for entry in os.listdir(path):
        target = os.path.join(path, entry)
        try:
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            else:
                os.remove(target)
            cleared = True
        except OSError:
            log.exception("could not clear %s during account purge", target)
    return cleared


def _strip_matrix_refs(path: str) -> bool:
    """Blank the Matrix room/event on pending self-destructs, keep the TG side.

    The Telegram deletion is the point of the feature and still has to happen;
    the Matrix event it also pointed at is gone with the old account.
    """
    data = _read_json(path)
    if not isinstance(data, list) or not data:
        return False
    changed = False
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("mx_room") or item.get("mx_event"):
            item["mx_room"] = None
            item["mx_event"] = None
            changed = True
    return _write_json(path, data) if changed else False


def _strip_active_targets(path: str) -> bool:
    """Drop the room→target map; every key is a room of the old account.

    Deliberately surgical: everything else in state.json describes the Telegram
    account, which has not changed.
    """
    data = _read_json(path)
    if not isinstance(data, dict) or not data.get("active"):
        return False
    data["active"] = {}
    return _write_json(path, data)


def _read_json(path: str) -> Optional[object]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (json.JSONDecodeError, OSError):
        log.warning("unreadable %s during account purge", path)
        return None


def _write_json(path: str, data: object) -> bool:
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError:
        log.exception("could not rewrite %s during account purge", path)
        return False
