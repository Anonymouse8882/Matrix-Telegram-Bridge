"""The Telegram account vault: several accounts, all online at once.

One Matrix account controls the bridge; the *Telegram* side is where multiple
accounts live. Each one:

  * has its own Telethon session (`accounts/tg-<id>/telegram.session`),
  * is bound to its own Matrix **Space**, so its per-chat rooms are filed
    under that space and two accounts can never share a room,
  * owns its own caches (room map, message links, send queue) in that same
    directory, which is what makes the accounts independent by construction
    rather than by remembering to keep them apart.

`telegram_accounts.json` records the list and which one is *current* — the
account that control-room commands act on when no room says otherwise.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import time
from dataclasses import asdict, dataclass, replace
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_FILENAME = "telegram_accounts.json"
SESSION_FILENAME = "telegram.session"


@dataclass(frozen=True)
class TelegramAccount:
    """One logged-in Telegram account. Holds no secret: the session file does."""

    tg_id: int
    name: str = ""
    username: str = ""
    phone: str = ""
    space_id: str = ""  # bound Matrix Space; "" = unbound (rooms go to control)
    # This account's own control room — normally a room inside its Space, so
    # each account is driven from where its chats live. Empty falls back to the
    # root control room from config.
    control_room: str = ""
    last_used: float = 0.0

    @property
    def label(self) -> str:
        """How the account is named in a room: display name, handle, or id."""
        if self.name and self.username:
            return f"{self.name} (@{self.username})"
        return self.name or (f"@{self.username}" if self.username else str(self.tg_id))


def slug(tg_id: int) -> str:
    return f"tg-{int(tg_id)}"


def account_dir(data_dir: str, tg_id: int) -> str:
    return os.path.join(data_dir, "accounts", slug(tg_id))


def session_path(data_dir: str, tg_id: int) -> str:
    return os.path.join(account_dir(data_dir, tg_id), SESSION_FILENAME)


# sqlite keeps the database plus these side files. A session moved without them
# strands a partial transaction; a *stale* one left behind next to a new
# database is worse, because sqlite will try to recover it.
SESSION_SUFFIXES = ("", "-journal", "-wal", "-shm")


def install_session(scratch_dir: str, data_dir: str, tg_id: int) -> str:
    """Move a freshly signed-in session into its account directory.

    Only the session files move. The account's caches — room map, message
    links, settings, queues — are keyed by *Telegram chat id*, which signing in
    again does not change, so replacing the whole directory (as this used to
    do) would orphan every Matrix room the account already has and throw away
    its mutes, watch list and self-destruct TTLs for no reason. Re-logging in
    after a revoked session is a repair, not a fresh start.
    """
    target = account_dir(data_dir, tg_id)
    os.makedirs(target, exist_ok=True)
    # Clear the old session first, side files included: a leftover journal
    # recovered against the new database corrupts it.
    for suffix in SESSION_SUFFIXES:
        old = os.path.join(target, SESSION_FILENAME + suffix)
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                log.exception("could not remove the previous session file %s", old)
    for suffix in SESSION_SUFFIXES:
        src = os.path.join(scratch_dir, SESSION_FILENAME + suffix)
        if os.path.exists(src):
            os.replace(src, os.path.join(target, SESSION_FILENAME + suffix))
    shutil.rmtree(scratch_dir, ignore_errors=True)
    return target


# -- reading / writing ------------------------------------------------------


def load(path: str) -> list[TelegramAccount]:
    """Every saved account, most recently used first.

    A corrupt vault must not stop the bridge: it degrades to "no accounts",
    which the operator can fix by logging in again.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except (json.JSONDecodeError, OSError):
        log.exception("failed to read the telegram account vault at %s", path)
        return []

    raw = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[TelegramAccount] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            tg_id = int(item["tg_id"])
        except (KeyError, TypeError, ValueError):
            continue  # an entry with no id names no session
        out.append(TelegramAccount(
            tg_id=tg_id,
            name=str(item.get("name") or ""),
            username=str(item.get("username") or ""),
            phone=str(item.get("phone") or ""),
            space_id=str(item.get("space_id") or ""),
            control_room=str(item.get("control_room") or ""),
            last_used=float(item.get("last_used") or 0.0),
        ))
    out.sort(key=lambda a: -a.last_used)
    return out


def save_all(path: str, accounts: list[TelegramAccount]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"accounts": [asdict(a) for a in accounts]}, fh,
                  ensure_ascii=False, indent=2)
        fh.write("\n")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - NTFS ignores POSIX bits
        pass
    os.replace(tmp, path)


def upsert(path: str, account: TelegramAccount, now: float = 0.0) -> None:
    """Add or update an account, keeping every other one untouched."""
    accounts = [a for a in load(path) if a.tg_id != account.tg_id]
    stamped = replace(account, last_used=account.last_used or now or time.time())
    save_all(path, [stamped] + accounts)


def touch(path: str, tg_id: int, now: float = 0.0) -> None:
    accounts = load(path)
    for i, a in enumerate(accounts):
        if a.tg_id == tg_id:
            accounts[i] = replace(a, last_used=now or time.time())
            save_all(path, accounts)
            return


def bind_space(path: str, tg_id: int, space_id: str) -> Optional[TelegramAccount]:
    """Point an account at a Space (or clear it with an empty string)."""
    return _amend(path, tg_id, space_id=space_id)


def set_control_room(path: str, tg_id: int, room_id: str) -> Optional[TelegramAccount]:
    """Point an account at the room it is driven from."""
    return _amend(path, tg_id, control_room=room_id)


def _amend(path: str, tg_id: int, **fields) -> Optional[TelegramAccount]:
    accounts = load(path)
    updated = None
    for i, a in enumerate(accounts):
        if a.tg_id == tg_id:
            updated = replace(a, **fields)
            accounts[i] = updated
            break
    if updated is not None:
        save_all(path, accounts)
    return updated


def remove(path: str, tg_id: int) -> Optional[TelegramAccount]:
    accounts = load(path)
    gone = next((a for a in accounts if a.tg_id == tg_id), None)
    if gone is None:
        return None
    save_all(path, [a for a in accounts if a.tg_id != tg_id])
    return gone


def find(path: str, query: str) -> Optional[TelegramAccount]:
    """Resolve a numeric id, an @username, a display name, or a list position.

    The list position is what makes `!tg switch 2` work straight off the
    printed list, which is why the list is numbered.
    """
    return match(load(path), query)


def adopt_legacy_caches(data_dir: str, tg_id: int) -> list[str]:
    """Move a single-account layout's caches under `accounts/tg-<id>/`.

    Two older layouts have to be recognised. The first kept everything at the
    root of the data volume; the second (briefly) filed it under the *Matrix*
    account's name. Either way the caches describe the one Telegram account
    that was running, so they belong to it now.

    Moved rather than copied: a leftover copy at the old path would be adopted
    a second time later and hand out a duplicate set of rooms.
    """
    target = account_dir(data_dir, tg_id)
    os.makedirs(target, exist_ok=True)
    moved: list[str] = []
    # state.json is *copied*, not moved: its command prefix is still global,
    # while its mutes, watch list, delay and TTLs now belong to this account.
    root_state = os.path.join(data_dir, "state.json")
    account_state = os.path.join(target, "state.json")
    if os.path.exists(root_state) and not os.path.exists(account_state):
        try:
            shutil.copyfile(root_state, account_state)
            moved.append("state.json (设置已复制给该账户)")
        except OSError:
            log.exception("could not copy %s", root_state)
    for source in _legacy_dirs(data_dir):
        for name in ("rooms.json", "msglinks.json", "outbox.json", "expire.json"):
            src = os.path.join(source, name)
            dst = os.path.join(target, name)
            if os.path.exists(src) and not os.path.exists(dst):
                try:
                    os.replace(src, dst)
                    moved.append(name)
                except OSError:
                    log.exception("could not adopt %s", src)
    return moved


def _legacy_dirs(data_dir: str) -> list[str]:
    """Where a pre-multi-account install may have left its caches."""
    found = [data_dir]
    parent = os.path.join(data_dir, "accounts")
    if os.path.isdir(parent):
        found += [
            os.path.join(parent, name)
            for name in sorted(os.listdir(parent))
            # Anything not named `tg-<id>` predates Telegram accounts owning
            # these directories, so it is a leftover to be drained.
            if not name.startswith("tg-") and not name.startswith(".")
            and os.path.isdir(os.path.join(parent, name))
        ]
    return found


def match(accounts: list[TelegramAccount], query: str) -> Optional[TelegramAccount]:
    q = (query or "").strip()
    if not q:
        return None
    bare = q.lstrip("@").lower()
    # An exact id or username always wins: internal lookups resolve accounts by
    # their tg_id as a string, which must never be misread as a list position.
    for a in accounts:
        if str(a.tg_id) == bare or a.username.lower() == bare:
            return a
    # A short pure number is a list position, as printed by `accounts`.
    # Telegram ids are far above any plausible list length.
    if q.isdigit() and len(q) <= 3:
        idx = int(q) - 1
        return accounts[idx] if 0 <= idx < len(accounts) else None
    for a in accounts:
        if a.name.lower() == bare:
            return a
    for a in accounts:
        if bare and bare in a.name.lower():
            return a
    return None
