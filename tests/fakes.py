"""In-memory test doubles implementing the core ports.

Because the core depends only on ports, these fakes let us exercise the entire
dispatch / relay logic with zero network and zero SDKs.
"""

from __future__ import annotations

from bridge.core.models import (
    Dialog,
    DialogSummary,
    MediaRef,
    OutboundMessage,
    TelegramTarget,
)
from bridge.core.ports import MessageHandler


class FakeSource:
    def __init__(self) -> None:
        self.handler: MessageHandler | None = None
        self.started = False

    def set_handler(self, handler: MessageHandler) -> None:
        self.handler = handler

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        pass


class FakeFetcher:
    def __init__(self, payload: bytes = b"BYTES") -> None:
        self.payload = payload
        self.calls: list[MediaRef] = []

    async def fetch(self, ref: MediaRef) -> bytes:
        self.calls.append(ref)
        return self.payload


class RecordingSink:
    def __init__(self, return_id=None) -> None:
        self.deliveries: list[tuple[TelegramTarget, OutboundMessage]] = []
        self.return_id = return_id

    async def deliver(
        self, target: TelegramTarget, message: OutboundMessage
    ):
        self.deliveries.append((target, message))
        return self.return_id

    async def close(self) -> None:
        pass


class FakeExpirer:
    def __init__(self) -> None:
        self.scheduled: list[tuple[int, int, float]] = []
        self.matrix: list[tuple] = []  # (matrix_room, matrix_event) per schedule

    async def schedule(self, chat_id, msg_id, delay_seconds,
                       matrix_room=None, matrix_event=None) -> None:
        self.scheduled.append((chat_id, msg_id, delay_seconds))
        self.matrix.append((matrix_room, matrix_event))


class FakeSender:
    """An OutboundSender that records submissions; scheduled if `at` given."""

    def __init__(self) -> None:
        self.submissions: list[tuple] = []
        self.origins: list = []       # origin_event per submission
        self.names: list = []         # target_name per submission
        self.origin_rooms: list = []  # origin_room per submission

    async def submit(self, chat_id, dialog_kind, message, at=None,
                     origin_event=None, target_name="", origin_room=None):
        self.submissions.append((chat_id, dialog_kind, message, at))
        self.origins.append(origin_event)
        self.names.append(target_name)
        self.origin_rooms.append(origin_room)
        if at is not None:
            return ("scheduled", at)
        return ("sent", None)


class FakeRoomCreator:
    """A RoomCreator handing out sequential room ids; can be told to fail."""

    def __init__(self, fail: bool = False) -> None:
        self.created: list = []  # the Dialogs rooms were created for
        self.topics: list = []   # (room_id, ChatInfo) from set_topic
        self.avatars: list = []  # (room_id, chat_id) from set_avatar
        self.avatar_result = "set"  # what set_avatar reports back
        self.names: list = []    # (room_id, name) from set_name
        self.fail = fail
        self._n = 0

    async def create_chat_room(self, dialog) -> str:
        if self.fail:
            raise RuntimeError("room creation refused (test)")
        self._n += 1
        self.created.append(dialog)
        room_id = f"!room{self._n}:test"
        await self.set_avatar(room_id, dialog.id)  # as the real adapter does
        return room_id

    async def set_topic(self, room_id: str, info) -> None:
        self.topics.append((room_id, info))

    async def set_avatar(self, room_id: str, chat_id) -> str:
        self.avatars.append((room_id, chat_id))
        return self.avatar_result

    async def set_name(self, room_id: str, name: str) -> bool:
        self.names.append((room_id, name))
        return True


class FakeAccounts:
    """An `AccountRouter` over one (or more) fake Telegram accounts.

    Most tests only care about a single account, so the common case is
    `FakeAccounts(directory, sender)` and everything else is defaulted.
    """

    def __init__(
        self,
        directory=None,
        sender=None,
        registry=None,
        links=None,
        reply_map=None,
        rooms=None,
        account=None,
        state=None,
        extra: list | None = None,
    ) -> None:
        from bridge.accounts import TelegramAccount
        from bridge.core.messagelinks import MessageLinks
        from bridge.core.ports import AccountBundle
        from bridge.core.replymap import ReplyMap
        from bridge.core.roomregistry import RoomRegistry
        from bridge.core.state import BridgeState

        self.account = account or TelegramAccount(
            tg_id=1001, name="Me", username="me", phone="+100"
        )
        self.state = state if state is not None else BridgeState()
        self._bundle = AccountBundle(
            account=self.account,
            directory=directory if directory is not None else FakeDirectory(),
            sender=sender if sender is not None else FakeSender(),
            registry=registry if registry is not None else RoomRegistry(),
            links=links if links is not None else MessageLinks(),
            reply_map=reply_map if reply_map is not None else ReplyMap(),
            state=self.state,
            control_room=self.account.control_room,
            rooms=rooms,
        )
        self._extra = extra or []          # additional bundles, for routing tests
        # Saved accounts with no live runtime (a revoked session). They are
        # listed by `accounts()` but have no bundle, like the real manager.
        self.offline: list = []
        self._current = self._bundle
        # Recorded calls, so command tests can assert without a live client.
        self.logins: list[tuple[str, str]] = []
        self.codes: list[str] = []
        self.passwords: list[str] = []
        self.switched: list[str] = []
        self.bound: list[tuple[str, str]] = []
        self.controls: list[tuple[str, str]] = []
        self.logged_out: list[str] = []
        # A login is pending by default — that is the state the code/2fa steps
        # are exercised in; tests for "nothing pending" clear it explicitly.
        self.stage = "code"
        self.result = None  # AccountResult returned by the operations

    # -- lookup --------------------------------------------------------------

    def _all(self) -> list:
        return [self._bundle] + self._extra

    def accounts(self) -> list:
        return [b.account for b in self._all()] + self.offline

    def is_online(self, tg_id: int) -> bool:
        return not any(a.tg_id == int(tg_id) for a in self.offline)

    def current(self):
        return self._current

    def set_current(self, bundle) -> None:
        self._current = bundle

    def for_room(self, room_id: str):
        for bundle in self._all():
            if bundle.registry.chat_for(room_id) is not None:
                return bundle
        return None

    def by_query(self, query: str):
        for bundle in self._all():
            a = bundle.account
            if query in (str(a.tg_id), a.username, a.name):
                return bundle
        return None

    # -- operations ----------------------------------------------------------

    def _reply(self, **kw):
        from bridge.core.models import AccountResult
        return self.result or AccountResult(ok=True, **kw)

    async def begin_login(self, phone: str, space_id: str, control_room: str = ""):
        self.logins.append((phone, space_id, control_room))
        self.stage = "code"
        return self._reply(stage="code")

    async def submit_code(self, code: str):
        self.codes.append(code)
        return self._reply(stage="done", label=self.account.label, detail="")

    async def submit_password(self, password: str):
        self.passwords.append(password)
        return self._reply(stage="done", label=self.account.label, detail="")

    def pending_login(self) -> str:
        return self.stage

    def cancel_login(self) -> None:
        self.stage = ""

    async def switch_to(self, query: str):
        self.switched.append(query)
        return self._reply(label=self.account.label)

    async def bind_space(self, query: str, space_id: str):
        self.bound.append((query, space_id))
        return self._reply(label=self.account.label, detail=space_id)

    async def set_control_room(self, query: str, room_id: str):
        self.controls.append((query, room_id))
        return self._reply(label=self.account.label, detail=room_id)

    def for_control_room(self, room_id: str):
        for bundle in self._all():
            if bundle.control_room and bundle.control_room == room_id:
                return bundle
        return None

    async def logout(self, query: str):
        self.logged_out.append(query)
        return self._reply(label=self.account.label)


class FakeSpaces:
    """A `SpaceResolver` over a fixed room -> space map."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = mapping or {}

    async def space_for_room(self, room_id: str):
        return self.mapping.get(room_id)


class FakeDirectory:
    """A TelegramDirectory over a fixed list of dialogs."""

    def __init__(self, dialogs: list[Dialog] | None = None) -> None:
        self.dialogs = dialogs or []
        self.dms: list[DialogSummary] = []
        self.resolve_calls: list[tuple[str, str | None]] = []
        self.history_rows: list[tuple[str, str]] = []
        self.history_limits: list[int] = []  # limits history() was called with
        self.infos: dict = {}       # query (bare) -> ChatInfo for info()
        self.info_calls: list[str] = []
        self.join_result = None     # Dialog returned by join()
        self.join_calls: list[str] = []
        self.left: list[int] = []   # chat ids leave() was called on
        self.leave_raises = False
        self.stats: list[tuple[Dialog, int]] = []
        self.deleted: list[int] = []  # chat ids delete_own_messages was called on
        self.deleted_single: list[tuple[int, int]] = []  # (chat, msg) deletes
        self.delete_raises = False    # simulate a permission failure
        self.avatars: dict[int, bytes] = {}  # chat id -> photo bytes
        self.avatar_calls: list[int] = []

        self.presences: dict[int, str] = {}  # chat id -> "ok"/"deleted"/"gone"

    async def avatar(self, chat_id: int):
        self.avatar_calls.append(chat_id)
        return self.avatars.get(int(chat_id))

    async def presence(self, chat_id: int) -> str:
        return self.presences.get(int(chat_id), "ok")

    async def list_dialogs(self) -> list[Dialog]:
        return list(self.dialogs)

    async def list_dms(self) -> list[DialogSummary]:
        return list(self.dms)

    async def info(self, query: str):
        self.info_calls.append(query)
        return self.infos.get(query.lstrip("@"))

    async def join(self, query: str):
        self.join_calls.append(query)
        return self.join_result

    async def leave(self, chat_id: int) -> None:
        if self.leave_raises:
            raise RuntimeError("not a member")
        self.left.append(chat_id)

    async def resolve(self, query: str, kind: str | None = None):
        self.resolve_calls.append((query, kind))
        # Mirror the real adapter: "@name" and "name" are the same lookup, and
        # usernames are matched as well as display names.
        bare = query.lstrip("@").lower()
        pool = [d for d in self.dialogs if kind is None or d.kind == kind]
        for d in pool:
            if bare in (str(d.id), (d.username or "").lower(), d.name.lower()):
                return d
        for d in pool:  # substring match on name, like the real adapter
            if bare and bare in d.name.lower():
                return d
        return None

    async def history(self, query: str, limit: int):
        self.history_limits.append(limit)
        return self.history_rows[:limit]

    async def own_message_stats(self):
        return list(self.stats)

    async def delete_own_messages(self, chat_id: int) -> int:
        self.deleted.append(chat_id)
        return 3  # pretend we deleted 3 per chat

    async def delete_message(self, chat_id: int, msg_id: int) -> None:
        if self.delete_raises:
            raise RuntimeError("no permission")
        self.deleted_single.append((chat_id, msg_id))

