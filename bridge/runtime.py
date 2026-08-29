"""One Telegram account, wired end to end — and the router over all of them.

`AccountRuntime` owns everything that belongs to a single Telegram account: its
Telethon client, the relay that carries its messages into Matrix, the queue
that carries them back, and the caches describing *its* rooms. Nothing is
shared between accounts except the one Matrix client and the control room, so
two accounts can be online at once without their rooms, links or send queues
ever meeting.

`AccountManager` is the `AccountRouter` port: it starts and stops runtimes,
resolves which account a room belongs to, and drives the interactive login.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Callable, Optional

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from . import accounts as vault
from .accounts import TelegramAccount
from .adapters.matrix_rooms import MatrixRooms
from .adapters.outbound_scheduler import OutboundScheduler
from .adapters.telegram_expirer import TelegramExpirer
from .adapters.telegram_quotly import QuotLyQuoter
from .adapters.telegram_user_sink import TelegramUserSink
from .adapters.telegram_user_source import TelegramUserSource
from .config import Config
from .core.messagelinks import MessageLinks
from .core.models import AccountResult
from .core.ports import AccountBundle
from .core.relay import Relay
from .core.replymap import ReplyMap
from .core.roomregistry import RoomRegistry
from .core.state import BridgeState
from .proxy import parse_proxy, telethon_proxy

log = logging.getLogger(__name__)


def _client_for(cfg: Config, session: str) -> TelegramClient:
    """A Telethon client pinned to a generic device profile.

    Telethon otherwise reports the real platform and uname to Telegram, which
    fingerprints the host — and every account would report the same one,
    linking them together.
    """
    os.makedirs(os.path.dirname(session) or ".", exist_ok=True)
    return TelegramClient(
        session,
        cfg.telegram.api_id,
        cfg.telegram.api_hash,
        proxy=telethon_proxy(parse_proxy(cfg.proxy.url)),
        device_model=cfg.telegram.device_model,
        system_version=cfg.telegram.system_version,
        app_version=cfg.telegram.app_version,
        lang_code=cfg.telegram.lang_code,
        system_lang_code=cfg.telegram.lang_code,
    )


class AccountRuntime:
    """The live machinery of one Telegram account."""

    def __init__(
        self,
        account: TelegramAccount,
        cfg: Config,
        data_dir: str,
        client: TelegramClient,
        matrix_client,
        matrix_sink,
        matrix_fetcher,
        root_control_room: str,
        homeserver_name: str,
        on_new_room: Callable[[str], None],
    ):
        self._matrix_client = matrix_client
        self._homeserver_name = homeserver_name
        self._root_control_room = root_control_room
        self.account = account
        self.client = client
        self._dir_path = vault.account_dir(data_dir, account.tg_id)
        os.makedirs(self._dir_path, exist_ok=True)

        self.registry = RoomRegistry(os.path.join(self._dir_path, "rooms.json"))
        self.links = MessageLinks(os.path.join(self._dir_path, "msglinks.json"))
        self.reply_map = ReplyMap()
        # Settings belong to the account, not the bridge: a self-destruct TTL
        # or a mute is about *this* Telegram identity, and two accounts sharing
        # them would be a surprise every time one is configured.
        state = BridgeState(os.path.join(self._dir_path, "state.json"))
        self.state = state

        self.sink = TelegramUserSink(0, "", client=client)
        self.source = TelegramUserSource(client)
        # Only used while this account's forward mode is QuotLy.
        self.quoter = QuotLyQuoter(client, bot=cfg.options.quotly_bot)
        # Per-chat rooms need a Space to live in; without one this account
        # relays into the control room, which is degraded but never lossy.
        self.rooms = (
            MatrixRooms(matrix_client, account.space_id, homeserver_name,
                        directory=self.source)
            if account.space_id
            else None
        )
        self.expirer = TelegramExpirer(
            client, os.path.join(self._dir_path, "expire.json")
        )
        self.scheduler = OutboundScheduler(
            telegram_sink=self.sink,
            matrix_fetcher=matrix_fetcher,
            state=state,
            expirer=self.expirer,
            path=os.path.join(self._dir_path, "outbox.json"),
            reply_map=self.reply_map,
            control_room=self.control_room,
            links=self.links,
            quoter=self.quoter,
        )
        self.relay = Relay(
            matrix_sink=matrix_sink,
            telegram_fetcher=self.source,
            state=state,
            control_room=self.control_room,
            reply_map=self.reply_map,
            registry=self.registry if self.rooms else None,
            rooms=self.rooms,
            on_new_room=on_new_room,
            links=self.links,
            editor=matrix_sink,
        )
        self.expirer.set_marker(self.relay.on_telegram_deleted)
        self.source.set_handler(self.relay.on_telegram_message)
        self.source.set_delete_handler(self.relay.on_telegram_deleted)
        self.source.set_edit_handler(self.relay.on_telegram_edited)
        self._tasks: list[asyncio.Task] = []

    @property
    def directory(self):
        return self.source

    @property
    def control_room(self) -> str:
        """Where this account is driven from: its own room, else the root one."""
        return self.account.control_room or self._root_control_room

    def bundle(self) -> AccountBundle:
        return AccountBundle(
            account=self.account,
            directory=self.source,
            sender=self.scheduler,
            registry=self.registry,
            links=self.links,
            reply_map=self.reply_map,
            state=self.state,
            control_room=self.control_room,
            rooms=self.rooms,
        )

    def rebind(self, account: TelegramAccount) -> None:
        """Adopt a new Space or control room without restarting the client.

        Rooms already created stay where they are — a rebind changes where the
        *next* ones are filed, not the history.
        """
        self.account = account
        self.rooms = (
            MatrixRooms(self._matrix_client, account.space_id,
                        self._homeserver_name, directory=self.source)
            if account.space_id
            else None
        )
        self.relay.set_rooms(self.registry if self.rooms else None, self.rooms)
        self.relay.set_control_room(self.control_room)

    def start(self) -> list[asyncio.Task]:
        label = str(self.account.tg_id)
        self._tasks = [
            _observed(asyncio.create_task(self.source.start()), f"tg-source {label}"),
            _observed(asyncio.create_task(self.expirer.run()), f"expirer {label}"),
            _observed(asyncio.create_task(self.scheduler.run()), f"scheduler {label}"),
        ]
        return self._tasks

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        try:
            await self.source.close()
        except Exception:  # noqa: BLE001 - shutting down anyway
            log.debug("error closing telegram client for %s", self.account.tg_id)
        # Only after the source: its per-chat drain tasks are cancelled inside
        # close(), and an update still being drained must be able to record its
        # link. Closing first made `_save` a silent no-op for those.
        # This flushes the debounced writer and disarms it, so a timer cannot
        # fire after `logout` deletes the directory and re-create it.
        self.links.close()

    def wipe(self) -> str:
        """Delete this account's session and caches. Returns a human note."""
        return _wipe_account_dir(self._dir_path)


class AccountManager:
    """The `AccountRouter` port: every Telegram account, and how to reach it."""

    def __init__(
        self,
        cfg: Config,
        data_dir: str,
        matrix_client,
        matrix_sink,
        matrix_fetcher,
        root_control_room: str,
        homeserver_name: str,
        on_new_room: Callable[[str], None],
    ):
        self._cfg = cfg
        self._data_dir = data_dir
        self._vault_path = os.path.join(data_dir, vault.DEFAULT_FILENAME)
        self._matrix_client = matrix_client
        self._matrix_sink = matrix_sink
        self._matrix_fetcher = matrix_fetcher
        self._control_room = root_control_room
        self._homeserver = homeserver_name
        self._on_new_room = on_new_room

        self._runtimes: dict[int, AccountRuntime] = {}
        self._current: Optional[int] = None
        self._pending: Optional[_PendingLogin] = None
        # Live references to fire-and-forget cleanup tasks, so the loop's weak
        # reference is not the only one keeping them alive.
        self._closers: set[asyncio.Task] = set()
        # The vault, cached: `accounts()` is called on hot paths (redactions,
        # settings) and every mutation goes through this class, so re-reading
        # the file each time is wasted disk I/O.
        self._vault_cache: Optional[list[TelegramAccount]] = None

    def _vault(self) -> list[TelegramAccount]:
        if self._vault_cache is None:
            self._vault_cache = vault.load(self._vault_path)
        return self._vault_cache

    def _vault_changed(self) -> None:
        self._vault_cache = None

    # -- lifecycle -----------------------------------------------------------

    async def start_all(self) -> list[asyncio.Task]:
        """Bring every saved account online. One bad session must not stop
        the others — a single unusable account is a problem for that account."""
        tasks: list[asyncio.Task] = []
        await self._adopt_legacy_session()
        for account in self._vault():
            try:
                runtime = await self._spawn(account)
            except Exception:  # noqa: BLE001
                log.exception("could not start telegram account %s", account.tg_id)
                continue
            tasks.extend(runtime.start())
        if self._runtimes and self._current is None:
            self._current = next(iter(self._runtimes))
        log.info("telegram accounts online: %d", len(self._runtimes))
        return tasks

    async def stop_all(self) -> None:
        for runtime in list(self._runtimes.values()):
            await runtime.stop()
        self._runtimes.clear()
        if self._pending is not None:
            await self._pending.close()
            self._pending = None
        if self._closers:
            # Let any cancel_login cleanup finish before the loop goes away.
            await asyncio.gather(*self._closers, return_exceptions=True)
            self._closers.clear()

    async def _adopt_legacy_session(self) -> None:
        """Turn a pre-multi-account install into account number one.

        Before this, the bridge ran one Telegram session named in config.yaml
        and used one Space, also from config. Both become an ordinary account
        so nothing has to be logged in again, and the rooms already created
        keep working — losing the room map would re-create every room.
        """
        if self._vault():
            return  # already migrated, or a fresh install that logged in
        legacy = self._cfg.telegram.session
        if not legacy or not os.path.exists(legacy):
            return

        client = _client_for(self._cfg, legacy)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                log.warning("legacy telegram session %s is not authorised", legacy)
                return
            me = await client.get_me()
        except Exception:  # noqa: BLE001 - never block startup on migration
            log.exception("could not read the legacy telegram session")
            return
        finally:
            await _safe_disconnect(client)

        tg_id = int(me.id)
        name = " ".join(
            p for p in (getattr(me, "first_name", ""), getattr(me, "last_name", ""))
            if p
        ).strip()
        target = vault.session_path(self._data_dir, tg_id)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            # The journal is sqlite's write-ahead file; leaving it behind would
            # strand a partial transaction next to a session that moved away.
            for suffix in ("", "-journal", "-wal", "-shm"):
                if os.path.exists(legacy + suffix):
                    os.replace(legacy + suffix, target + suffix)
        except OSError:
            log.exception("could not move the legacy session into place")
            return
        moved = vault.adopt_legacy_caches(self._data_dir, tg_id)
        account = TelegramAccount(
            tg_id=tg_id, name=name, username=getattr(me, "username", "") or "",
            phone=self._cfg.telegram.phone,
            # The single configured Space and control room become this
            # account's, so the install carries on exactly as before.
            space_id=self._cfg.matrix.space,
            control_room=self._cfg.matrix.control_room,
        )
        vault.upsert(self._vault_path, account)
        self._vault_changed()
        log.info("adopted the existing telegram session as account %s (%s); "
                 "moved %s", account.label, tg_id, ", ".join(moved) or "nothing")

    async def _spawn(self, account: TelegramAccount) -> AccountRuntime:
        session = vault.session_path(self._data_dir, account.tg_id)
        client = _client_for(self._cfg, session)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(f"session for {account.tg_id} is not authorised")
        runtime = AccountRuntime(
            account=account,
            cfg=self._cfg,
            data_dir=self._data_dir,
            client=client,
            matrix_client=self._matrix_client,
            matrix_sink=self._matrix_sink,
            matrix_fetcher=self._matrix_fetcher,
            root_control_room=self._control_room,
            homeserver_name=self._homeserver,
            on_new_room=self._on_new_room,
        )
        self._runtimes[account.tg_id] = runtime
        if self._current is None:
            self._current = account.tg_id
        return runtime

    def watched_rooms(self) -> set[str]:
        """Every room the bridge drives, for the Matrix sync filter: each
        account's per-chat rooms and its own control room."""
        rooms: set[str] = set()
        for runtime in self._runtimes.values():
            rooms |= runtime.registry.rooms()
            if runtime.account.control_room:
                rooms.add(runtime.account.control_room)
        return rooms

    def for_control_room(self, room_id: str) -> Optional[AccountBundle]:
        """The account driven from this room, if it is a control room."""
        for runtime in self._runtimes.values():
            if runtime.account.control_room == room_id:
                return runtime.bundle()
        return None

    async def set_control_room(self, query: str, room_id: str) -> AccountResult:
        account = self._account_by_query(query)
        if account is None:
            return AccountResult(ok=False, error=f"没有这个账户：{query or '（当前）'}")
        clash = next(
            (a for a in self.accounts()
             if a.control_room == room_id and a.tg_id != account.tg_id),
            None,
        )
        if room_id and clash is not None:
            # Two accounts driven from one room could not tell whose command
            # is whose, so the second binding has to be refused.
            return AccountResult(
                ok=False,
                error=f"这个房间已经是 {clash.label} 的控制房间了，先给它换一个。",
            )
        updated = vault.set_control_room(self._vault_path, account.tg_id, room_id)
        self._vault_changed()
        if updated is None:
            return AccountResult(ok=False, error="保存失败。")
        runtime = self._runtimes.get(account.tg_id)
        if runtime is not None:
            runtime.rebind(updated)
        if room_id:
            self._on_new_room(room_id)  # the source must sync it from now on
        return AccountResult(ok=True, label=updated.label, detail=room_id)

    # -- AccountRouter: lookup ----------------------------------------------

    def accounts(self) -> list[TelegramAccount]:
        """Every saved account, whether or not it came online.

        Filtering to live runtimes used to hide exactly the account that most
        needs attention: one whose session Telegram revoked never enters
        `_runtimes`, and hiding it made `accounts`, `switch` and above all
        `logout` unable to name it, leaving the vault entry only removable by
        hand-editing the file.
        """
        saved = self._vault()
        known = {a.tg_id for a in saved}
        # A runtime with no vault entry should not happen; never hide one if
        # it does, or it becomes unmanageable in the other direction.
        return saved + [
            r.account for r in self._runtimes.values() if r.account.tg_id not in known
        ]

    def is_online(self, tg_id: int) -> bool:
        return int(tg_id) in self._runtimes

    def _account_by_query(self, query: str) -> Optional[TelegramAccount]:
        """Resolve a query against every saved account, live or not.

        `by_query` answers with a live *bundle* and so cannot see a dead
        account; management commands need the account record itself.
        """
        if query:
            return vault.match(self.accounts(), query)
        bundle = self.current()
        return bundle.account if bundle is not None else None

    def current_id(self) -> Optional[int]:
        return self._current

    def current(self) -> Optional[AccountBundle]:
        runtime = self._runtimes.get(self._current) if self._current else None
        return runtime.bundle() if runtime else None

    def for_room(self, room_id: str) -> Optional[AccountBundle]:
        for runtime in self._runtimes.values():
            if runtime.registry.chat_for(room_id) is not None:
                return runtime.bundle()
        return None

    def by_query(self, query: str) -> Optional[AccountBundle]:
        account = vault.match(self.accounts(), query)
        if account is None:
            return None
        runtime = self._runtimes.get(account.tg_id)
        return runtime.bundle() if runtime else None

    # -- AccountRouter: login ------------------------------------------------

    def pending_login(self) -> str:
        return self._pending.stage if self._pending else ""

    def cancel_login(self) -> None:
        """Abandon a pending login, from a sync context."""
        pending, self._pending = self._pending, None
        if pending is None:
            return
        # Hold a reference: the loop keeps only a weak one, so an unreferenced
        # task can be collected before it has closed the client and removed the
        # scratch session. `_observed` makes a failure visible rather than lost.
        task = _observed(asyncio.create_task(pending.close()), "login cancel")
        self._closers.add(task)
        task.add_done_callback(self._closers.discard)

    async def begin_login(
        self, phone: str, space_id: str, control_room: str = ""
    ) -> AccountResult:
        if self._pending is not None:
            await self._pending.close()
        # The Telegram id is only known after sign-in, so the session starts in
        # a scratch directory and moves to `tg-<id>/` once we know the name.
        scratch = os.path.join(self._data_dir, "accounts", ".pending")
        shutil.rmtree(scratch, ignore_errors=True)
        client = _client_for(self._cfg, os.path.join(scratch, vault.SESSION_FILENAME))
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except Exception as exc:  # noqa: BLE001
            log.exception("send_code_request failed for a login attempt")
            await _safe_disconnect(client)
            return AccountResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        self._pending = _PendingLogin(
            client=client, phone=phone, hash=sent.phone_code_hash,
            space_id=space_id, scratch=scratch, control_room=control_room,
        )
        return AccountResult(ok=True, stage="code")

    async def submit_code(self, code: str) -> AccountResult:
        pending = self._pending
        if pending is None:
            return AccountResult(ok=False, error="没有正在进行的登录。")
        try:
            await pending.client.sign_in(
                phone=pending.phone, code=code, phone_code_hash=pending.hash
            )
        except SessionPasswordNeededError:
            pending.stage = "password"
            return AccountResult(ok=True, stage="password")
        except Exception as exc:  # noqa: BLE001
            log.exception("sign_in with code failed")
            return AccountResult(ok=False, stage="code",
                                 error=f"{type(exc).__name__}: {exc}")
        return await self._finish_login(pending)

    async def submit_password(self, password: str) -> AccountResult:
        pending = self._pending
        if pending is None:
            return AccountResult(ok=False, error="没有正在进行的登录。")
        try:
            await pending.client.sign_in(password=password)
        except Exception as exc:  # noqa: BLE001
            log.exception("sign_in with 2fa password failed")
            return AccountResult(ok=False, stage="password",
                                 error=f"{type(exc).__name__}: {exc}")
        return await self._finish_login(pending)

    async def _finish_login(self, pending: "_PendingLogin") -> AccountResult:
        """Name the account, move its session into place, and bring it online."""
        me = await pending.client.get_me()
        tg_id = int(me.id)
        name = " ".join(
            p for p in (getattr(me, "first_name", ""), getattr(me, "last_name", ""))
            if p
        ).strip()
        account = TelegramAccount(
            tg_id=tg_id, name=name, username=getattr(me, "username", "") or "",
            phone=pending.phone, space_id=pending.space_id,
            # The room the login was run in becomes this account's control
            # room, so it is driven from inside its own Space by default.
            control_room=pending.control_room,
        )
        # The session file cannot move while it is open.
        await _safe_disconnect(pending.client)
        self._pending = None

        if tg_id in self._runtimes:  # re-login of an account already online
            await self._runtimes.pop(tg_id).stop()
        # Only the session moves; this account's caches stay exactly where
        # they are (see vault.install_session).
        vault.install_session(pending.scratch, self._data_dir, tg_id)

        vault.upsert(self._vault_path, account)
        self._vault_changed()
        try:
            runtime = await self._spawn(account)
        except Exception as exc:  # noqa: BLE001
            log.exception("could not start the account just logged in")
            return AccountResult(ok=False, label=account.label,
                                 error=f"登录成功但启动失败：{exc}")
        runtime.start()
        self._current = tg_id
        if account.control_room:
            self._on_new_room(account.control_room)
        parts = [
            f"已绑定空间 {account.space_id}" if account.space_id
            else "未绑定空间：对话会进入控制房间，用 bind 绑定一个空间",
        ]
        parts.append(
            f"本房间已设为该账户的控制房间" if account.control_room
            else "沿用全局控制房间（用 control 指定一个）"
        )
        return AccountResult(ok=True, stage="done", label=account.label,
                             detail="；".join(parts))

    # -- AccountRouter: management ------------------------------------------

    async def switch_to(self, query: str) -> AccountResult:
        account = self._account_by_query(query)
        if account is None:
            return AccountResult(ok=False, error=f"没有这个账户：{query}")
        if not self.is_online(account.tg_id):
            return AccountResult(ok=False, error=(
                f"{account.label} 不在线（会话已失效或未能启动），不能切换过去。"
                f"重新登录，或用 logout 移除它。"
            ))
        if account.tg_id == self._current:
            return AccountResult(ok=False, error=f"{account.label} 已经是当前账户。")
        self._current = account.tg_id
        vault.touch(self._vault_path, account.tg_id)
        self._vault_changed()
        return AccountResult(ok=True, label=account.label)

    async def bind_space(self, query: str, space_id: str) -> AccountResult:
        account = self._account_by_query(query)
        if account is None:
            return AccountResult(ok=False, error=f"没有这个账户：{query or '（当前）'}")
        clash = next(
            (a for a in self.accounts()
             if a.space_id and a.space_id == space_id and a.tg_id != account.tg_id),
            None,
        )
        if space_id and clash is not None:
            # One space per account, or two accounts' rooms would interleave
            # under the same heading with no way to tell them apart.
            return AccountResult(
                ok=False,
                error=f"这个空间已经绑给 {clash.label} 了，先解绑再试。",
            )
        updated = vault.bind_space(self._vault_path, account.tg_id, space_id)
        self._vault_changed()
        if updated is None:
            return AccountResult(ok=False, error="保存绑定失败。")
        runtime = self._runtimes.get(account.tg_id)
        if runtime is not None:
            runtime.rebind(updated)
        return AccountResult(ok=True, label=updated.label, detail=space_id)

    async def logout(self, query: str) -> AccountResult:
        account = self._account_by_query(query)
        if account is None:
            return AccountResult(ok=False, error=f"没有这个账户：{query or '（当前）'}")
        runtime = self._runtimes.pop(account.tg_id, None)
        note = ""
        if runtime is not None:
            try:
                # Sign out at Telegram too: deleting the local session alone
                # would leave an authorised device on the account for ever.
                await runtime.client.log_out()
            except Exception:  # noqa: BLE001
                log.warning("telegram log_out failed for %s", account.tg_id)
                note = "⚠️ 未能在 Telegram 端注销这个会话，请在手机上手动删除该设备。"
            await runtime.stop()
            wiped = runtime.wipe()
        else:
            # Never came online — a revoked session, say. There is no client to
            # sign out with, but the files are still on disk and clearing them
            # is the whole point of logout.
            note = ("⚠️ 该账户不在线，无法在 Telegram 端注销。"
                    "如果那个设备还在，请在手机上手动删除它。")
            wiped = _wipe_account_dir(
                vault.account_dir(self._data_dir, account.tg_id)
            )
        note = f"{note}\n{wiped}".strip() if wiped else note
        vault.remove(self._vault_path, account.tg_id)
        self._vault_changed()
        if self._current == account.tg_id:
            self._current = next(iter(self._runtimes), None)
        return AccountResult(ok=True, label=account.label, detail=note)


class _PendingLogin:
    """A half-finished login: the client is connected, waiting for a code."""

    def __init__(self, client, phone: str, hash: str, space_id: str,
                 scratch: str, control_room: str = ""):
        self.client = client
        self.phone = phone
        self.hash = hash
        self.space_id = space_id
        self.scratch = scratch
        self.control_room = control_room
        self.stage = "code"

    async def close(self) -> None:
        await _safe_disconnect(self.client)
        shutil.rmtree(self.scratch, ignore_errors=True)


def _observed(task: asyncio.Task, label: str) -> asyncio.Task:
    """Log a background task's death instead of losing the exception.

    Tasks started for an account logged in at runtime are not awaited by
    anything, so without this an account could silently stop syncing.
    """
    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.error("background task %s died: %r", label, exc)

    task.add_done_callback(_done)
    return task


async def _safe_disconnect(client) -> None:
    try:
        if client.is_connected():
            await client.disconnect()
    except Exception:  # noqa: BLE001
        log.debug("error disconnecting a telegram client")


def _wipe_account_dir(path: str) -> str:
    """Delete an account's session and caches. Returns a human note.

    Shared so an account that never came online can still be cleared: its
    files exist whether or not a runtime was built for it.
    """
    if not os.path.isdir(path):
        return ""
    try:
        shutil.rmtree(path)
        return "已删除该账户的会话文件和本地缓存（房间映射、消息链接、发送队列）"
    except OSError:
        log.exception("could not wipe %s", path)
        return "⚠️ 缓存删除失败，见日志"
