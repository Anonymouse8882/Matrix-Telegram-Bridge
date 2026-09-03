"""Outgoing dispatcher: Matrix -> Telegram, across several Telegram accounts.

Interprets what the owner types:
  * `<prefix> ...`      -> a control command (list / use / at / login / ...)
  * `@target message`   -> send `message` to `target` for this one message
  * anything else       -> send to the room's Telegram chat, or to the current
                           account's active target in the control room

Which *account* acts is decided per message, never held as state on this class:
a per-chat room belongs to exactly one account, and control-room commands use
the current one. That is what keeps several accounts online without their
rooms, queues or targets ever mixing.

Every Telegram send goes through that account's OutboundSender (its scheduler),
which applies send-delay / scheduled-time / self-destruct uniformly. Pure
orchestration over ports.
"""

from __future__ import annotations

import logging
import re
import shlex
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from .duration import format_duration, parse_duration
from .models import Dialog, InboundMessage, MessageKind, OutboundMessage, Target
from .ports import (
    AccountBundle,
    AccountRouter,
    MatrixRedactor,
    MessageSink,
    SpaceResolver,
)
from .render import note, panel
from .replymap import ReplyRef
from .tgerrors import failure_reason
from .transformer import info_lines
from .state import FORWARD_NORMAL, FORWARD_QUOTLY, BridgeState

log = logging.getLogger(__name__)

_KIND_ICON = {"user": "👤", "bot": "🤖", "group": "👥", "channel": "📢"}
# Why a chat is gone, in the owner's words. Same vocabulary as `presence()` and
# `failure_reason()`, so a chat found dead by a sweep and one found dead by a
# refused send are reported identically. Two phrasings because the same fact
# reads differently after a name ("小明（账户已注销）") and after a colon
# ("发送失败：对方账户已注销"), and there it must be clear whose account it was.
_GONE_REASON = {"deleted": "账户已注销", "gone": "对话已不存在"}
_GONE_SEND_FAIL = {"deleted": "对方账户已注销", "gone": "该对话已不存在"}
_KIND_TITLE = {"user": "私信", "bot": "机器人", "group": "群组", "channel": "频道"}
# The order kinds are listed in, everywhere.
_KINDS = ("user", "bot", "group", "channel")

_KIND_ALIASES = {
    "dm": "user", "private": "user", "私信": "user", "user": "user",
    "bot": "bot", "机器人": "bot", "bots": "bot",
    "group": "group", "群": "group", "群组": "group",
    "channel": "channel", "频道": "channel",
}

# `fmsg` modes. The names are the ones the user types, in any case.
_FMSG_ALIASES = {
    "normal": FORWARD_NORMAL, "普通": FORWARD_NORMAL, "正常": FORWARD_NORMAL,
    "off": FORWARD_NORMAL,
    "quotly": FORWARD_QUOTLY, "quote": FORWARD_QUOTLY, "语录": FORWARD_QUOTLY,
}
_FMSG_TITLE = {
    FORWARD_NORMAL: "Normal（原样发送）",
    FORWARD_QUOTLY: "QuotLy（转成语录贴纸）",
}

_BULK_SCOPES = {
    "allchannel": ("channel",),
    "allgroup": ("group",),
    "alluser": ("user",),
    "allbot": ("bot",),
    "allchat": ("user", "bot", "group", "channel"),
}

_HELP_LINES = [
    "list                列出所有对话（私信/机器人/群组/频道分组显示）",
    "dms [N]             列出真人私信（未读优先，含最后一条摘要）",
    "dm <目标> [N]       查看某个私信的内容（默认 20 条）",
    "use <目标>          设为当前发送目标",
    "who                 显示当前发送目标",
    "read <目标> [N]     查看某对话最近消息",
    "info <目标>         查看用户/群组/频道信息（专属房内可省略目标）",
    "join <@用户名|邀请链接>   加入群组/频道",
    "leave <群组/频道> confirm 退出群组/频道（专属房内可省略目标）",
    "stats               查看你在哪些对话有记录及条数",
    "settings            查看当前账户的所有设置",
    "prefix <新前缀>     自定义命令前缀（默认 !tg）",
    "",
    "── Telegram 账户（可多个同时在线）──",
    "accounts            列出已登录的 TG 账户",
    "login <手机号>      登录新账户（在目标空间的房间里发，自动绑定该空间）",
    "  code <验证码> / 2fa <密码>   按提示继续登录",
    "switch <序号|账户>  切换全局房间操作的账户（各账户有自己的控制房间）",
    "bind [账户]         把账户绑到本房间所在的空间 · unbind 解绑",
    "control [账户]      把本房间设为该账户的控制房间 · control show 查看",
    "logout [账户] confirm   退出并删除该账户的会话与本地缓存",
    "",
    "room <目标>         为对话预建专属房间 · rooms 查看已建",
    "                    （建了专属房间的群/频道自动转发，无需 watch）",
    "avatar [目标|all]   同步 TG 头像到专属房间（建房时自动设一次）",
    "                    专属房内直接 avatar 即同步该对话",
    "check [目标|all]    检查对话是否还在，房名标记【已注销】/（已删除）",
    "                    （对方注销账户/对话不存在时；恢复后自动还原房名）",
    "                    发送失败若是对方已注销，会自动标记，无需手动 check",
    "watch/unwatch <群/频道/机器人>  接收白名单增删",
    "                    （真人私信默认转发；机器人必须 watch 才转发）",
    "watching                 查看接收白名单",
    "mute/unmute <目标>       是否提醒（仍显示）· muted 查看",
    "",
    "at <YYYY-MM-DD> <HH:MM[:SS]> <内容>   定时发到当前目标",
    "fmsg [Normal|QuotLy]     发送模式：原样发送 / 转成 @QuotLyBot 语录贴纸",
    "delay                    查看发送延迟",
    "delay <固定> [随机]      设置延迟, 如 delay 5s 30s (0=关)",
    "selfdestruct [<类型> <时长>]  自毁设置(类型 私信/群组/频道)",
    "delMsg <目标|AllUser|AllBot|AllGroup|AllChannel|AllChat>  删自己的消息(需 confirm)",
    "         专属房内直接 delMsg confirm 即删该对话里你的全部消息",
    "",
    "@目标 内容          临时发给指定目标",
    "直接发消息/图片     发给当前目标（受延迟设置影响）",
    "在 Element 里回复某条转发消息 → 作为回复发回该 TG 对话",
]

_AT_RE = re.compile(
    r"^\S+\s+at\s+(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2}(?::\d{2})?)\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)

# Commands whose argument is a secret, so the message carrying it is redacted
# the moment it is seen — before anything else is attempted.
_SECRET_COMMANDS = {"code", "2fa", "password", "验证码", "密码"}

# The active-target slot inside an account's own state file.
_ACTIVE_TARGET = "@current"


def parse_options(text: str, prefix: str) -> tuple[list[str], dict[str, str]]:
    """Split `<prefix> sub a b space=!x:hs` into positional args and options.

    Options are `key=value` anywhere in the line, so `login`'s phone number and
    `bind`'s account can be given in whatever order reads naturally.
    """
    rest = text[len(prefix):].strip()
    _, _, rest = rest.partition(" ")  # drop the sub-command word
    args: list[str] = []
    opts: dict[str, str] = {}
    for token in rest.split():
        key, sep, value = token.partition("=")
        if sep and key:
            opts[key.lower()] = value
        else:
            args.append(token)
    return args, opts


class Dispatcher:
    def __init__(
        self,
        accounts: AccountRouter,
        matrix_replier: MessageSink,
        state: BridgeState,
        control_room: str,
        command_prefix: str = "!tg",
        default_target: str = "",
        timezone: str = "UTC",
        on_new_room: Optional[Callable[[str], None]] = None,
        redactor: MatrixRedactor | None = None,
        spaces: SpaceResolver | None = None,
    ):
        self._accounts = accounts
        self._mx = matrix_replier
        self._state = state
        self._room = control_room
        self._default_prefix = command_prefix
        self._on_new_room = on_new_room
        self._redactor = redactor  # deletes messages that carried a secret
        self._spaces = spaces
        self._default_target = default_target
        self._tz_name = timezone
        try:
            self._tz = ZoneInfo(timezone)
        except Exception:  # noqa: BLE001 - bad tz name -> fall back to UTC
            log.warning("unknown timezone %r, using UTC", timezone)
            self._tz = ZoneInfo("UTC")
            self._tz_name = "UTC"

    @property
    def _prefix(self) -> str:
        """The active command prefix: a `!tg prefix` override, else the config
        default. Read dynamically so a change takes effect immediately."""
        return self._state.command_prefix() or self._default_prefix

    # -- account resolution --------------------------------------------------

    def _current(self) -> Optional[AccountBundle]:
        return self._accounts.current()

    def _control_bundle(self, room: str) -> Optional[AccountBundle]:
        """The account a control room drives.

        Each account normally has its own control room inside its Space; the
        room from config is the *root* one and drives whichever account is
        current, so account management still works before any account exists.
        """
        bundle = self._accounts.for_control_room(room)
        if bundle is not None:
            return bundle
        return self._current() if room == self._room else None

    def _is_control_room(self, room: str) -> bool:
        return room == self._room or self._accounts.for_control_room(room) is not None

    async def _need_account(self, room: Optional[str] = None) -> Optional[AccountBundle]:
        bundle = self._current()
        if bundle is None:
            await self._reply_in(room or self._room, note(
                f"还没有登录任何 Telegram 账户。用 {self._prefix} login <手机号> 登录，"
                f"或 {self._prefix} accounts 查看。"
            ))
        return bundle

    # -- entry point ---------------------------------------------------------

    async def on_matrix_message(self, msg: InboundMessage) -> None:
        text = (msg.text or "").strip()
        is_command = msg.kind is MessageKind.TEXT and text.startswith(self._prefix)
        room = msg.source_room

        if not self._is_control_room(room):
            await self._on_other_room_message(msg, text, is_command)
            return

        if is_command:
            # The event id travels with the command so a secret-bearing one can
            # be deleted from the room.
            await self._handle_command(text, msg.event_id, room)
            return

        bundle = self._control_bundle(room)
        if bundle is None:
            bundle = await self._need_account(room)
        if bundle is None:
            return

        # Native Element reply to a relayed message -> reply in that TG chat.
        if msg.reply_to_event:
            ref = self._reply_ref(bundle, msg.reply_to_event)
            if ref is None:
                # Deliberately does NOT fall through to the active target.
                # Element strips the quote from the body, so a reply re-routed
                # to whoever happens to be the current target would reach the
                # wrong person with nothing to show it went astray.
                await self._reply_in(room, note(
                    "找不到这条回复对应的 Telegram 消息（太旧、已清理，或它本来就"
                    "不是转发来的消息），没有发送。要发给当前目标请直接发消息，"
                    "不要用回复。"
                ))
                return
            out = OutboundMessage(
                kind=msg.kind,
                text=(text if msg.kind is MessageKind.TEXT else msg.text or None),
                media=msg.media,
                reply_to=ref.msg_id,
            )
            await self._deliver_to(
                bundle, ref.chat_id, ref.kind, ref.name, out,
                origin_event=msg.event_id, origin_room=room,
            )
            return

        if msg.kind is MessageKind.TEXT and text.startswith("@"):
            parts = text[1:].split(None, 1)
            body = parts[1] if len(parts) > 1 else ""
            if not body:
                await self._reply_in(room, note("用法：@目标 要发送的内容"))
                return
            # Keep the leading @ so the lookup knows this is a username, not a
            # display name. Numeric ids are passed bare - "@123" is no username.
            raw = parts[0]
            query = raw if raw.lstrip("-").isdigit() else f"@{raw}"
            await self._send(
                bundle, query, OutboundMessage(MessageKind.TEXT, text=body),
                origin_event=msg.event_id, origin_room=room, room=room,
            )
            return

        target_query = self._active_target(bundle)
        if not target_query:
            await self._reply_in(room, note(
                f"「{bundle.account.label}」还没有发送目标。用 {self._prefix} use <目标> "
                f"设置，或 {self._prefix} list 查看对话。"
            ))
            return

        if msg.kind is MessageKind.TEXT:
            out = OutboundMessage(MessageKind.TEXT, text=text)
        else:
            out = OutboundMessage(kind=msg.kind, text=msg.text or None, media=msg.media)
        await self._send(bundle, target_query, out, origin_event=msg.event_id,
                         origin_room=room, room=room)

    def _reply_ref(
        self, bundle: AccountBundle, event_id: str
    ) -> Optional[ReplyRef]:
        """Which Telegram message a replied-to Matrix event came from.

        The in-memory ReplyMap answers first, then the persisted MessageLinks.
        The second lookup is what keeps replies working across a restart: the
        ReplyMap starts empty there, and without a fallback every reply to an
        older message would have to be refused.
        """
        ref = bundle.reply_map.lookup(event_id) if bundle.reply_map else None
        if ref is not None:
            return ref
        link = bundle.links.by_event(event_id) if bundle.links else None
        if link is None:
            return None
        name = bundle.registry.name_for(link.chat_id) or str(link.chat_id)
        return ReplyRef(chat_id=link.chat_id, msg_id=link.msg_id,
                        kind=link.kind, name=name)

    def _active_target(self, bundle: AccountBundle) -> str:
        """The account's current chat. Its state is already per account, so one
        fixed key is enough — and it survives the control room being moved.
        The configured default applies until `use` picks something; a read
        never writes state."""
        return bundle.state.active_target(_ACTIVE_TARGET) or self._default_target or ""

    async def _on_other_room_message(
        self, msg: InboundMessage, text: str, is_command: bool
    ) -> None:
        """A message somewhere that is not the control room.

        Two possibilities: one of an account's per-chat rooms (type to send),
        or any other room the bridge is in — where only commands make sense,
        so an account can be logged in and bound from inside its own Space.
        """
        bundle = self._accounts.for_room(msg.source_room)
        if bundle is None:
            if is_command:
                await self._handle_command(text, msg.event_id, msg.source_room)
            return  # not ours: never forward stray text anywhere

        chat_id = bundle.registry.chat_for(msg.source_room)
        if chat_id is None:
            return

        # Guard: a command typed here was almost certainly meant for the
        # control room. Sending "!tg mute ..." to a real human would be an
        # embarrassing misfire, so intercept it instead of forwarding.
        if is_command:
            await self._chat_room_command(msg, text, bundle, chat_id)
            return

        # Native reply inside the room still threads back properly. Nothing is
        # refused here: the room already fixes the destination, so an
        # unresolvable reply can only lose its threading, never its recipient.
        reply_to = None
        if msg.reply_to_event:
            ref = self._reply_ref(bundle, msg.reply_to_event)
            if ref is not None and ref.chat_id == chat_id:
                reply_to = ref.msg_id

        kind, name = await self._chat_room_identity(bundle, chat_id, msg.source_room)
        out = OutboundMessage(
            kind=msg.kind,
            text=(text if msg.kind is MessageKind.TEXT else msg.text or None),
            media=msg.media,
            reply_to=reply_to,
        )
        await self._deliver_to(
            bundle, chat_id, kind, name or str(chat_id), out,
            origin_event=msg.event_id, origin_room=msg.source_room,
        )

    async def _chat_room_identity(
        self, bundle: AccountBundle, chat_id: int, room_id: str
    ) -> tuple[str, str]:
        """(kind, name) of a per-chat room's chat, without a per-message lookup.

        The registry answers from its cache; only a mapping created before the
        kind was recorded falls back to one directory lookup, whose result is
        then written back so it never happens again for this chat.
        """
        kind = bundle.registry.kind_for(chat_id)
        name = bundle.registry.name_for(chat_id)
        if kind:
            return kind, name
        dialog = await self._resolve(bundle, str(chat_id))
        kind = dialog.kind if dialog else ("user" if chat_id > 0 else "group")
        name = (dialog.name if dialog else "") or name
        bundle.registry.register(chat_id, room_id, name, kind=kind)
        return kind, name

    async def _chat_room_command(
        self, msg: InboundMessage, text: str, bundle: AccountBundle, chat_id: int
    ) -> None:
        sub, _, arg = text[len(self._prefix):].strip().partition(" ")
        sub = sub.lower()
        room = msg.source_room
        if sub in ("info", "whois"):
            await self._cmd_info(bundle, arg.strip(), room=room, default_chat=chat_id)
        elif sub in ("avatar", "头像"):
            await self._cmd_avatar(bundle, arg.strip(), room=room,
                                   default_chat=chat_id)
        elif sub in ("check", "检查"):
            await self._cmd_check(bundle, arg.strip(), room=room,
                                  default_chat=chat_id)
        elif sub == "delmsg":
            # In-room delMsg always means THIS chat, so the only argument that
            # counts is the confirmation — a stray target must not widen it.
            confirm = "confirm" if "confirm" in arg.lower().split() else ""
            await self._cmd_delmsg(bundle, "", confirm, room=room,
                                   default_chat=chat_id)
        elif sub in ("leave", "quit", "退出群", "离开"):
            confirm = "confirm" if "confirm" in arg.lower().split() else ""
            await self._cmd_leave(bundle, "", confirm, room=room,
                                  default_chat=chat_id)
        elif sub in _SECRET_COMMANDS:
            # Whatever it was, it was a secret and it is now in a room shared
            # with a real person. Delete first, explain second.
            await self._redact_command(msg.event_id, room=room)
            await self._reply_in(room, note("账户登录请到全局房间或空间里的房间进行。"))
        else:
            await self._reply_in(room, note(
                "这里直接打字即发送；命令请到控制房间使用"
                "（本房可用：info、avatar、check、delMsg、leave）。"
            ))

    async def on_redaction(self, event_id: str) -> None:
        """Owner deleted a message in Element -> delete the mapped TG message."""
        for bundle in self._all_bundles():
            ref = bundle.reply_map.lookup(event_id)
            if ref is None:
                continue
            # Drop the link first: deleting on Telegram echoes a delete update
            # back, and marking an event the owner just redacted would error.
            bundle.links.forget(ref.chat_id, ref.msg_id)
            try:
                await bundle.directory.delete_message(ref.chat_id, ref.msg_id)
            except Exception:  # noqa: BLE001 - usually a permissions problem
                log.warning("delete_message failed for %s/%s",
                            ref.chat_id, ref.msg_id)
                await self._say(bundle.control_room, note(
                    f"⚠️ 无法删除「{ref.name}」里的那条 TG 消息（可能无删除权限）。"
                ))
            return  # unmapped elsewhere: never touch unrelated messages

    def _all_bundles(self) -> list[AccountBundle]:
        out = []
        for account in self._accounts.accounts():
            bundle = self._accounts.by_query(str(account.tg_id))
            if bundle is not None:
                out.append(bundle)
        return out

    # -- sending -------------------------------------------------------------

    async def _resolve(
        self, bundle: AccountBundle, query: str, kind: Optional[str] = None
    ) -> Optional[Dialog]:
        try:
            return await bundle.directory.resolve(query, kind)
        except Exception:  # noqa: BLE001
            log.exception("resolve failed for %r", query)
            return None

    async def _send(
        self,
        bundle: AccountBundle,
        target_query: str,
        message: OutboundMessage,
        at: Optional[float] = None,
        origin_event: Optional[str] = None,
        origin_room: Optional[str] = None,
        room: str = "",
    ) -> None:
        dialog = await self._resolve(bundle, target_query)
        if dialog is None:
            await self._say(room, note(f"找不到目标：{target_query}"))
            return
        await self._deliver_to(
            bundle, dialog.id, dialog.kind, dialog.name, message, at, origin_event,
            origin_room=origin_room,
        )

    async def _deliver_to(
        self,
        bundle: AccountBundle,
        chat_id: int,
        kind: str,
        name: str,
        message: OutboundMessage,
        at: Optional[float] = None,
        origin_event: Optional[str] = None,
        origin_room: Optional[str] = None,
    ) -> None:
        # Feedback goes where the user typed, not always to the control room.
        reply_room = origin_room or self._room
        try:
            status, when = await bundle.sender.submit(
                chat_id, kind, message, at,
                origin_event=origin_event, target_name=name,
                origin_room=origin_room or self._room,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("submit to %s failed", chat_id)
            await self._report_send_failure(bundle, chat_id, name, reply_room, exc)
            return
        if status == "scheduled" and when is not None:
            await self._reply_in(
                reply_room, note(f"⏳ 已排期 {self._fmt(when)} 发送到 {name}")
            )
        elif at is not None:
            await self._reply_in(
                reply_room, note(f"时间已过，已立即发送到 {name}。")
            )

    async def on_send_failed(
        self,
        tg_id: int,
        chat_id: int,
        name: str,
        origin_room: str,
        exc: BaseException,
    ) -> None:
        """A *queued* send was given up on (see OutboundScheduler).

        The immediate path reports its own failures, where the owner is still
        looking; a delayed one fails minutes later with nobody watching, so it
        gets the same explanation — and the same room mark — after the fact.
        """
        bundle = self._accounts.by_query(str(tg_id))
        if bundle is None:
            log.warning("no account %s to report a failed send for", tg_id)
            return
        room = origin_room or bundle.control_room or self._room
        await self._report_send_failure(
            bundle, chat_id, name or str(chat_id), room, exc
        )

    async def _report_send_failure(
        self,
        bundle: AccountBundle,
        chat_id: int,
        name: str,
        reply_room: str,
        exc: BaseException,
    ) -> None:
        """Say *why* a send failed, and flag the room when the chat is dead.

        "见日志" is the right answer only when nothing better is known. When
        Telegram itself says the other side deleted their account, the message
        is never going to arrive — so the owner is told that in the room they
        typed in, and the chat's room is renamed on the spot instead of waiting
        for someone to think of running `check`.
        """
        reason = failure_reason(exc)
        if reason == "gone":
            # A peer-invalid error alone is not proof: a stale access hash
            # looks the same. Let the account itself say whether the chat is
            # really gone before any room gets renamed over it.
            status = await self._presence(bundle, chat_id)
            reason = status if status in _GONE_REASON else ""
        if reason == "self":
            await self._reply_in(reply_room, note(
                f"发送到 {name} 失败：本 Telegram 账户已被注销或封禁，"
                f"需重新登录（{bundle.account.label}）。"
            ))
            return
        if reason not in _GONE_REASON:
            await self._reply_in(reply_room, note(f"发送到 {name} 失败，见日志。"))
            return

        _marked, renamed = await self._mark_gone(bundle, chat_id, reason, name)
        lines = [f"❌ 发送到 {name} 失败：{_GONE_SEND_FAIL[reason]}，消息无法送达。"]
        if renamed:
            lines.append(f"已把房间改名为「{renamed}」。")
        lines.append("（房间和聊天记录都保留；对方若恢复，收到新消息会自动还原房名）")
        await self._reply_in(reply_room, note("\n".join(lines)))

    async def _presence(self, bundle: AccountBundle, chat_id: int) -> str:
        """`presence()` without letting its own failure become the answer."""
        try:
            return await bundle.directory.presence(chat_id)
        except Exception:  # noqa: BLE001
            log.exception("presence check failed for %s", chat_id)
            return "ok"

    def _fmt(self, epoch: float) -> str:
        return datetime.fromtimestamp(epoch, self._tz).strftime("%Y-%m-%d %H:%M:%S")

    # -- command dispatch ----------------------------------------------------

    async def _handle_command(
        self, text: str, event_id: Optional[str], room: str
    ) -> None:
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
        sub = tokens[1].lower() if len(tokens) > 1 else "help"
        arg = tokens[2] if len(tokens) > 2 else ""
        extra = tokens[3] if len(tokens) > 3 else ""

        # Account management works in any room the bridge is in, so an account
        # can be logged in from inside the Space it should be bound to.
        if sub in ("accounts", "account", "账户"):
            await self._cmd_accounts(room)
            return
        if sub in ("login", "登录"):
            await self._cmd_login(text, event_id, room)
            return
        if sub in _SECRET_COMMANDS:
            await self._cmd_login_step(sub, text, event_id, room)
            return
        if sub in ("switch", "切换"):
            await self._cmd_switch(arg, room)
            return
        if sub in ("bind", "绑定", "unbind", "解绑"):
            await self._cmd_bind(text, room, unbind=sub in ("unbind", "解绑"))
            return
        if sub in ("logout", "退出"):
            await self._cmd_logout(arg, extra, room)
            return

        if sub in ("control", "控制房", "主房间"):
            await self._cmd_control(text, room)
            return

        if not self._is_control_room(room):
            await self._reply_in(room, note(
                f"这个房间只支持账户相关命令（accounts / login / switch / "
                f"bind / control / logout）。其他命令请到控制房间。"
            ))
            return

        if sub in ("help", "?"):
            await self._reply_in(
                room, panel(f"命令帮助（前缀 {self._prefix}）", _HELP_LINES)
            )
            return
        if sub == "prefix":
            await self._cmd_prefix(arg, extra, room)
            return

        # Everything below acts on an account: the one this control room drives.
        bundle = self._control_bundle(room) or await self._need_account(room)
        if bundle is None:
            return

        # One row per command: aliases -> the call. Adding a command is adding
        # a row (plus a _HELP_LINES entry), not extending a branch chain.
        table = {
            ("fmsg", "发送模式"): lambda: self._cmd_fmsg(bundle, arg, room),
            ("delay",): lambda: self._cmd_delay(bundle, arg, extra, room),
            ("selfdestruct",): lambda: self._cmd_selfdestruct(bundle, arg, extra, room),
            ("settings", "status", "config"): lambda: self._cmd_settings(bundle, room),
            ("list", "ls"): lambda: self._cmd_list(bundle, room),
            ("dms", "pm", "私信"): lambda: self._cmd_dms(bundle, arg, room),
            ("dm",): lambda: self._cmd_dm(bundle, arg, extra, room),
            ("use",): lambda: self._cmd_use(bundle, arg, room),
            ("who",): lambda: self._cmd_who(bundle, room),
            ("read",): lambda: self._cmd_read(bundle, arg, extra, room),
            ("stats",): lambda: self._cmd_stats(bundle, room),
            ("at",): lambda: self._cmd_at(bundle, text, room),
            ("mute",): lambda: self._cmd_mute(bundle, arg, mute=True, room=room),
            ("unmute",): lambda: self._cmd_mute(bundle, arg, mute=False, room=room),
            ("muted",): lambda: self._cmd_muted(bundle, room),
            ("watch",): lambda: self._cmd_watch(bundle, arg, watch=True, room=room),
            ("unwatch",): lambda: self._cmd_watch(bundle, arg, watch=False, room=room),
            ("watching",): lambda: self._cmd_watching(bundle, room),
            ("delmsg",): lambda: self._cmd_delmsg(bundle, arg, extra, room=room),
            ("room",): lambda: self._cmd_room(bundle, arg, room),
            ("rooms",): lambda: self._cmd_rooms(bundle, room),
            ("avatar", "头像"): lambda: self._cmd_avatar(bundle, arg, room),
            ("check", "检查"): lambda: self._cmd_check(bundle, arg, room),
            ("info", "whois"): lambda: self._cmd_info(bundle, arg, room=room),
            ("join",): lambda: self._cmd_join(bundle, arg, room),
            ("leave", "quit", "退出群", "离开"):
                lambda: self._cmd_leave(bundle, arg, extra, room),
        }
        for names, run in table.items():
            if sub in names:
                await run()
                return
        await self._say(room, note(
            f"未知命令：{sub}。用 {self._prefix} help 查看用法。"
        ))

    # -- telegram accounts ---------------------------------------------------

    async def _cmd_accounts(self, room: str) -> None:
        saved = self._accounts.accounts()
        if not saved:
            await self._reply_in(room, note(
                f"还没有登录任何 Telegram 账户。"
                f"用 {self._prefix} login <手机号> 添加（在目标空间的房间里发，"
                f"会自动绑定那个空间）。"
            ))
            return
        current = self._current()
        current_id = current.account.tg_id if current else None
        lines: list[str] = []
        offline = 0
        for i, a in enumerate(saved, 1):
            live = self._accounts.is_online(a.tg_id)
            offline += 0 if live else 1
            mark = " ⭐当前" if a.tg_id == current_id else ""
            # An account whose session Telegram revoked is listed, not hidden:
            # it still owns a session file and caches, and saying so is what
            # makes it obvious why its chats stopped arriving.
            mark += "" if live else " ⚠️离线"
            lines.append(f"{i}. {a.label}{mark}")
            detail = [f"id {a.tg_id}"]
            if a.phone:
                detail.append(a.phone)
            detail.append(f"空间 {a.space_id}" if a.space_id else "⚠️ 未绑定空间")
            lines.append(f"    {' · '.join(detail)}")
        lines += [
            "",
            f"切换：{self._prefix} switch <序号|账户>",
            f"绑定空间：在该空间的房间里发 {self._prefix} bind",
            f"退出：{self._prefix} logout <序号|账户> confirm",
        ]
        if offline:
            lines += [
                "",
                f"⚠️ 有 {offline} 个账户离线（会话已失效或启动失败），收不到消息。",
                "   重新登录：python -m bridge.tglogin --config /config/config.yaml",
                "   （重新登录会保留它的专属房间和设置）",
                f"   或移除：{self._prefix} logout <序号|账户> confirm",
            ]
        await self._reply_in(room, panel(f"Telegram 账户 ({len(saved)})", lines))

    async def _cmd_login(self, text: str, event_id: Optional[str], room: str) -> None:
        """Start an interactive Telegram login for a new account."""
        args, opts = parse_options(text, self._prefix)
        phone = args[0] if args else ""
        if not phone:
            await self._reply_in(room, panel("登录 Telegram 账户", [
                f"{self._prefix} login <手机号>      例：{self._prefix} login +8613800138000",
                "",
                "在**目标空间下的任意房间**里发这条命令，登录成功后会自动把该空间",
                "绑给这个账户；也可以显式指定：space=<空间id>。",
                "",
                "接着按提示继续：",
                f"  {self._prefix} code 12345      Telegram 发来的验证码",
                f"  {self._prefix} 2fa <密码>      开了两步验证才需要",
                "",
                "⚠️ 验证码和两步验证密码会进入房间历史。命令消息会被立即撤回，",
                "   但服务器已经收到过 —— 介意就在服务器上跑",
                "   python -m bridge.tglogin --config /config/config.yaml",
            ]))
            return
        if not phone.lstrip("+").isdigit():
            await self._reply_in(room, note("手机号格式不对，例：+8613800138000"))
            return

        space_id = opts.get("space", "")
        if not space_id and room != self._room and self._spaces is not None:
            # The room this was typed in tells us which space to bind, which is
            # the whole point of allowing the command outside the control room.
            space_id = await self._spaces.space_for_room(room) or ""

        await self._reply_in(room, note(f"正在向 {phone} 请求验证码 …"))
        # The room this was typed in becomes the account's control room, so
        # each account is driven from inside its own Space.
        control = room if room != self._room else ""
        result = await self._accounts.begin_login(phone, space_id, control)
        if not result.ok:
            await self._reply_in(room, note(f"❌ 请求验证码失败：{result.error}"))
            return
        where = f"，登录后将绑定空间 {space_id}" if space_id else ""
        await self._reply_in(room, panel("等待验证码", [
            f"验证码已发往 {phone}{where}。",
            "",
            f"请发：{self._prefix} code <验证码>",
            "（这条命令会被自动撤回；验证码在 Telegram app 或短信里）",
        ]))

    async def _cmd_login_step(
        self, sub: str, text: str, event_id: Optional[str], room: str
    ) -> None:
        """Continue a pending login: the code, then a 2FA password if needed."""
        await self._redact_command(event_id, room=room)  # first, always
        args, _opts = parse_options(text, self._prefix)
        secret = args[0] if args else ""
        stage = self._accounts.pending_login()
        if not stage:
            await self._reply_in(room, note(
                f"没有正在进行的登录。先发 {self._prefix} login <手机号>。"
            ))
            return
        if not secret:
            await self._reply_in(room, note(f"用法：{self._prefix} {sub} <内容>"))
            return

        if sub in ("code", "验证码"):
            result = await self._accounts.submit_code(secret)
        else:
            result = await self._accounts.submit_password(secret)

        if not result.ok:
            await self._reply_in(room, note(f"❌ {result.error}"))
            return
        if result.stage == "password":
            await self._reply_in(room, note(
                f"这个账户开了两步验证。请发：{self._prefix} 2fa <密码>"
            ))
            return
        await self._reply_in(room, panel("登录成功", [
            f"✅ {result.label}",
            result.detail,
            "",
            f"它已成为当前账户。{self._prefix} accounts 查看全部。",
        ]))

    async def _cmd_switch(self, query: str, room: str) -> None:
        if not query:
            await self._reply_in(room, note(
                f"用法：{self._prefix} switch <序号|账户>"
                f"（{self._prefix} accounts 查看列表）"
            ))
            return
        result = await self._accounts.switch_to(query)
        if not result.ok:
            await self._reply_in(room, note(f"❌ {result.error}"))
            return
        await self._reply_in(room, note(
            f"✅ 当前账户：{result.label}"
            f"（其他账户仍然在线，各自的专属房照常收发）"
        ))

    async def _cmd_bind(self, text: str, room: str, unbind: bool) -> None:
        args, opts = parse_options(text, self._prefix)
        query = args[0] if args else ""
        if unbind:
            result = await self._accounts.bind_space(query, "")
            if not result.ok:
                await self._reply_in(room, note(f"❌ {result.error}"))
                return
            await self._reply_in(room, note(
                f"已解绑 {result.label} 的空间。它的新对话会进入全局房间，"
                f"已建好的专属房间不受影响。"
            ))
            return

        space_id = opts.get("space", "")
        if not space_id:
            if self._spaces is None:
                await self._reply_in(room, note("无法自动识别空间，请用 space=<空间id>。"))
                return
            space_id = await self._spaces.space_for_room(room) or ""
        if not space_id:
            await self._reply_in(room, note(
                "这个房间不在任何空间里，识别不到要绑哪个。"
                "请在目标空间下的房间里执行，或用 space=<空间id> 指定。"
            ))
            return
        result = await self._accounts.bind_space(query, space_id)
        if not result.ok:
            await self._reply_in(room, note(f"❌ {result.error}"))
            return
        await self._reply_in(room, note(
            f"✅ 已把「{result.label}」绑定到空间 {space_id}。"
            f"之后该账户的对话会在这个空间里建专属房间。"
        ))

    async def _cmd_control(self, text: str, room: str) -> None:
        """Make this room an account's control room.

        Each account is normally driven from a room inside its own Space, which
        is where `login` puts it automatically. This is the manual version, and
        the way to move one afterwards.
        """
        args, opts = parse_options(text, self._prefix)
        query = args[0] if args else ""
        if query.lower() in ("show", "查看"):
            lines = []
            for a in self._accounts.accounts():
                lines.append(f"{a.label} — {a.control_room or '（全局房间）'}")
            lines += ["", f"全局房间：{self._room}"]
            await self._reply_in(room, panel("控制房间", lines))
            return

        target_room = opts.get("room", room)
        result = await self._accounts.set_control_room(query, target_room)
        if not result.ok:
            await self._reply_in(room, note(f"❌ {result.error}"))
            return
        await self._reply_in(room, panel("控制房间", [
            f"✅ 「{result.label}」现在由这个房间控制：",
            f"   {target_room}",
            "",
            "在这里发命令就是操作这个账户 —— 设置（延迟、自毁、静音、白名单、",
            "当前目标）都是它自己的，跟别的账户互不影响。",
        ]))

    async def _cmd_logout(self, query: str, confirm: str, room: str) -> None:
        # `logout confirm` means "the current account, confirmed" - not "the
        # account named confirm", which would then never be found.
        if query.lower() == "confirm" and not confirm:
            query, confirm = "", "confirm"
        current = self._current()
        target = query or (current.account.label if current else "")
        if not target:
            await self._reply_in(room, note("没有可退出的账户。"))
            return
        if confirm.lower() != "confirm":
            await self._reply_in(room, panel("确认退出登录", [
                f"将要退出：{target}",
                "",
                "这会：在 Telegram 端注销该会话、删除本地会话文件和该账户的",
                "缓存（房间映射/消息链接/发送队列），并从账户列表移除。",
                "",
                "只是想换个账户操作的话用 switch —— 那个不删任何东西，",
                "所有账户也都保持在线。",
                "",
                f"确认请发：{self._prefix} logout {query or target} confirm",
            ]))
            return
        result = await self._accounts.logout(query)
        if not result.ok:
            await self._reply_in(room, note(f"❌ {result.error}"))
            return
        lines = [f"✅ 已退出：{result.label}"]
        if result.detail:
            lines.extend(result.detail.split("\n"))
        lines += [
            "",
            "⚠️ 仅清本地：已转发到 Matrix 房间里的消息仍在服务器上，需要自己删。",
        ]
        await self._reply_in(room, panel("退出登录", lines))

    async def _redact_command(
        self, event_id: Optional[str], room: Optional[str] = None
    ) -> None:
        """Delete a command message, so its secret stops being scrollback."""
        room = room or self._room
        if self._redactor is None or not event_id:
            await self._reply_in(room, note(
                "⚠️ 无法自动删除刚才那条命令，请手动删除（其中含验证码/密码）。"
            ))
            return
        try:
            await self._redactor.redact(room, event_id)
        except Exception:  # noqa: BLE001 - never let this abort the login
            log.warning("could not redact a secret-bearing command")
            await self._reply_in(room, note(
                "⚠️ 删除命令消息失败，请手动删除那条含验证码/密码的消息。"
            ))

    # -- per-account commands ------------------------------------------------

    async def _cmd_at(self, bundle: AccountBundle, text: str, room: str = "") -> None:
        m = _AT_RE.match(text)
        if not m:
            await self._say(room, note(
                f"用法：{self._prefix} at 2026-07-20 15:30 要发送的内容"
            ))
            return
        date_s, time_s, body = m.group(1), m.group(2), m.group(3).strip()
        epoch = self._parse_at(date_s, time_s)
        if epoch is None:
            await self._say(room, note("时间无效。格式：YYYY-MM-DD HH:MM[:SS]"))
            return
        target = self._active_target(bundle)
        if not target:
            await self._say(room, note(f"先用 {self._prefix} use <目标> 设置发送目标。"))
            return
        await self._send(
            bundle, target, OutboundMessage(MessageKind.TEXT, text=body), at=epoch,
            origin_room=room, room=room,
        )

    def _parse_at(self, date_s: str, time_s: str) -> Optional[float]:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                naive = datetime.strptime(f"{date_s} {time_s}", fmt)
            except ValueError:
                continue
            return naive.replace(tzinfo=self._tz).timestamp()
        return None

    async def _cmd_fmsg(self, bundle: AccountBundle, mode_arg: str,
                        room: str) -> None:
        """How this account's outgoing messages are rendered."""
        current = bundle.state.forward_mode()
        if not mode_arg:
            await self._reply_in(room, panel("发送模式（fmsg）", [
                f"当前：{_FMSG_TITLE.get(current, current)}",
                "",
                "Normal   原样发送你输入的内容",
                "QuotLy   先发给 @QuotLyBot 生成语录贴纸，删掉与机器人的往来消息后，"
                "把贴纸发给目标",
                "",
                f"设置：{self._prefix} fmsg <Normal|QuotLy>",
            ]))
            return
        mode = _FMSG_ALIASES.get(mode_arg.strip().lower())
        if mode is None:
            await self._reply_in(room, note(
                f"模式只能是 Normal 或 QuotLy。用法：{self._prefix} fmsg <Normal|QuotLy>"
            ))
            return
        bundle.state.set_forward_mode(mode)
        if mode == FORWARD_QUOTLY:
            await self._reply_in(room, note(
                "🖋 发送模式：QuotLy。之后发出的纯文字会先经 @QuotLyBot 转成语录贴纸"
                "再发给目标（图片/文件仍原样发送；机器人没回应时按原文发送）。"
            ))
        else:
            await self._reply_in(room, note("发送模式：Normal（原样发送）。"))

    async def _cmd_delay(self, bundle: AccountBundle, fixed_s: str,
                         random_s: str, room: str) -> None:
        if not fixed_s:
            f = bundle.state.delay_fixed()
            r = bundle.state.delay_random()
            if f == 0 and r == 0:
                await self._reply_in(room, note("发送延迟：关闭（消息立即转发）"))
                return
            await self._reply_in(room, panel("发送延迟", [
                f"固定：{format_duration(f)}",
                f"随机：0 ~ {format_duration(r)}",
                f"每条转发实际等待 = 固定 + 随机",
            ]))
            return
        fixed = parse_duration(fixed_s)
        rnd = parse_duration(random_s) if random_s else 0
        if fixed is None or rnd is None:
            await self._reply_in(room, note("时长格式无效。例：delay 5s 30s（固定5秒+随机0~30秒）。"))
            return
        bundle.state.set_delay(fixed, rnd)
        if fixed == 0 and rnd == 0:
            await self._reply_in(room, note("已关闭发送延迟。"))
        else:
            await self._reply_in(room, note(
                f"⏱ 发送延迟：固定 {format_duration(fixed)} + 随机 0~{format_duration(rnd)}"
            ))

    async def _cmd_list(self, bundle: AccountBundle, room: str = "") -> None:
        try:
            dialogs = await bundle.directory.list_dialogs()
        except Exception:  # noqa: BLE001
            log.exception("list_dialogs failed")
            await self._say(room, note("获取对话列表失败，见日志。"))
            return
        if not dialogs:
            await self._say(room, note("没有找到任何对话。"))
            return
        active = self._active_target(bundle)
        buckets: dict[str, list[str]] = {k: [] for k in _KINDS}
        for d in dialogs:
            marks = ""
            if active and active in (str(d.id), d.username):
                marks += " ⭐"
            if d.kind != "user" and self._relayed(bundle, d.id, d.kind):
                marks += " 👁"
            if bundle.state.is_muted(str(d.id)):
                marks += " 🔕"
            uname = f" @{d.username}" if d.username else ""
            buckets.get(d.kind, buckets["channel"]).append(
                f"  {d.name}{uname} — {d.id}{marks}"
            )
        lines: list[str] = []
        for kind in _KINDS:
            items = buckets[kind]
            lines.append(f"{_KIND_ICON[kind]} {_KIND_TITLE[kind]} ({len(items)})")
            lines.extend(items or ["  （无）"])
            lines.append("")
        lines.append("⭐当前  👁转发  🔕静音（机器人需 watch 才转发）")
        await self._say(room, panel(f"对话列表 · {bundle.account.label}", lines))

    async def _cmd_prefix(self, arg: str, extra: str = "", room: str = "") -> None:
        new = arg.strip()
        if not new:
            await self._reply_in(room, note(
                f"当前命令前缀：{self._prefix}\n用法：{self._prefix} prefix <新前缀>"
            ))
            return
        if extra or len(new.split()) > 1:
            await self._reply_in(room, note("前缀不能包含空格。"))
            return
        self._state.set_command_prefix(new)
        await self._reply_in(room, note(f"✅ 命令前缀已改为：{new}（例：{new} list）"))

    async def _cmd_info(
        self, bundle: AccountBundle, query: str, room: str,
        default_chat: int | None = None,
    ) -> None:
        target = query.strip()
        if not target and default_chat is not None:
            target = str(default_chat)
        if not target:
            await self._reply_in(room, note(f"用法：{self._prefix} info <目标>"))
            return
        try:
            info = await bundle.directory.info(target)
        except Exception:  # noqa: BLE001
            log.exception("info failed for %r", target)
            await self._reply_in(room, note("获取信息失败，见日志。"))
            return
        if info is None:
            await self._reply_in(room, note(f"找不到目标：{target}"))
            return
        await self._reply_in(room, panel(f"信息 · {info.title}", info_lines(info)))
        # Keep the dedicated room's topic and avatar fresh while we are here.
        if bundle.rooms is not None:
            room_id = bundle.registry.room_for(info.id)
            if room_id is not None:
                await bundle.rooms.set_topic(room_id, info)
                await bundle.rooms.set_avatar(room_id, info.id)

    async def _cmd_join(self, bundle: AccountBundle, query: str, room: str = "") -> None:
        if not query:
            await self._say(room, note(
                f"用法：{self._prefix} join <@用户名 | 邀请链接>"
            ))
            return
        await self._say(room, note(f"正在加入 {query} …"))
        try:
            dialog = await bundle.directory.join(query)
        except Exception:  # noqa: BLE001
            log.exception("join failed for %r", query)
            await self._say(room, note("加入失败，见日志（链接可能已失效或无效）。"))
            return
        if dialog is None:
            await self._say(room, note(f"加入失败：{query}（链接可能已失效或无效）。"))
            return
        uname = f" @{dialog.username}" if dialog.username else ""
        await self._say(room, note(
            f"✅ 已加入「{dialog.name}」{uname}（{dialog.id}）。"
            f"用 {self._prefix} use {dialog.id} 设为发送目标。"
        ))

    async def _cmd_leave(
        self,
        bundle: AccountBundle,
        query: str,
        confirm: str,
        room: str = "",
        default_chat: Optional[int] = None,
    ) -> None:
        """Leave a Telegram group or channel.

        Confirmed because it is not always undoable: a public chat can be
        rejoined by username, but a private one needs a fresh invite that only
        somebody still inside can issue.
        """
        if default_chat is not None and (not query or query.lower() == "confirm"):
            confirm = query or confirm
            query = str(default_chat)
        if not query:
            await self._say(room, note(f"用法：{self._prefix} leave <群组/频道>"))
            return
        dialog = await self._resolve(bundle, query)
        if dialog is None:
            await self._say(room, note(f"找不到目标：{query}"))
            return
        if dialog.kind == "user":
            # Telegram's "leave" for a DM deletes the conversation; refusing is
            # safer than silently doing something far more destructive.
            await self._say(room, note(
                f"{dialog.name} 是私信，不能退出。"
                f"（Telegram 的「退出私信」等于删除整段对话记录）"
            ))
            return

        rejoin = (f"公开的，之后可以用 {self._prefix} join @{dialog.username} 重新加入"
                  if dialog.username else "私有的，退出后需要别人重新邀请才能回去")
        if confirm.lower() != "confirm":
            how = (f"{self._prefix} leave confirm" if default_chat is not None
                   else f"{self._prefix} leave {query} confirm")
            await self._say(room, panel("确认退出", [
                f"将要退出：{dialog.name}（{dialog.id}）",
                f"这个{_KIND_TITLE[dialog.kind]}是{rejoin}。",
                "",
                f"确认请发：{how}",
            ]))
            return

        try:
            await bundle.directory.leave(dialog.id)
        except Exception:  # noqa: BLE001 - not a member, no permission, gone
            log.exception("leave failed for %s", dialog.id)
            await self._say(room, note(f"退出「{dialog.name}」失败，见日志。"))
            return
        lines = [f"✅ 已退出：{dialog.name}"]
        if bundle.registry.room_for(dialog.id):
            # The room is a record of what was said; leaving Telegram is no
            # reason to destroy it, and re-joining would reuse the mapping.
            lines.append("它的专属房间保留着（历史记录还在）；不想要的话在 Element "
                         "里删除即可。")
        await self._say(room, panel("退出", lines))

    async def _cmd_room(self, bundle: AccountBundle, query: str, room: str = "") -> None:
        if bundle.rooms is None:
            await self._say(room, note(
                f"「{bundle.account.label}」还没绑定空间，专属房间功能未启用。"
                f"在目标空间的房间里发 {self._prefix} bind 绑定。"
            ))
            return
        if not query:
            await self._say(room, note(f"用法：{self._prefix} room <目标>"))
            return
        dialog = await self._resolve(bundle, query)
        if dialog is None:
            await self._say(room, note(f"找不到目标：{query}"))
            return
        existing = bundle.registry.room_for(dialog.id)
        if existing:
            await self._say(room, note(f"「{dialog.name}」已有专属房间：{existing}"))
            return
        try:
            room_id = await bundle.rooms.create_chat_room(dialog)
        except Exception:  # noqa: BLE001
            log.exception("manual room creation failed for %s", dialog.name)
            await self._say(room, note(f"创建房间失败（{dialog.name}），见日志。"))
            return
        bundle.registry.register(dialog.id, room_id, dialog.name, kind=dialog.kind)
        if self._on_new_room is not None:
            self._on_new_room(room_id)
        if dialog.kind == "bot":
            # A bot relays on the allow-list alone, so a room made by hand
            # would otherwise sit empty for ever. Asking for the room is asking
            # for its messages.
            bundle.state.watch(str(dialog.id))
            await self._say(room, note(
                f"✅ 已为机器人「{dialog.name}」创建专属房间：{room_id}"
                f"（并已加入接收白名单，用 {self._prefix} unwatch {dialog.id} 可停止转发）"
            ))
            return
        await self._say(room, note(
            f"✅ 已为「{dialog.name}」创建专属房间：{room_id}"
            f"（有专属房间即会转发，无需再 watch）"
        ))

    # How a room whose Telegram chat is gone is renamed. The original name is
    # kept inside it so the room stays findable, and so the mark can be undone.
    # A deactivated account gets its own wording: "deleted the chat" and "left
    # Telegram entirely" are different facts, and only the second one is final.
    DELETED_PREFIX = "🗑 "
    DELETED_SUFFIX = "（已删除）"
    DEACTIVATED_SUFFIX = "【已注销】"

    def _gone_name(self, name: str, reason: str = "gone") -> str:
        suffix = (
            self.DEACTIVATED_SUFFIX if reason == "deleted" else self.DELETED_SUFFIX
        )
        return f"{self.DELETED_PREFIX}{name}{suffix}"

    async def _mark_gone(
        self, bundle: AccountBundle, chat_id: int, reason: str, name: str = ""
    ) -> tuple[bool, str]:
        """Flag a chat's room as gone.

        Returns (whether this call is what changed the mark, the room's new
        name — "" if nothing was renamed), so the caller can report only what
        actually happened.

        The mark is recorded first: the registry is what makes the rename
        happen exactly once, so a repeated failed send does not rewrite the
        same name on every message. An unbound account (no Space, so no rooms)
        still gets the mark — it is about the chat, not about the room.
        """
        base = bundle.registry.name_for(chat_id) or name
        if not bundle.registry.set_deleted(chat_id, True, reason):
            return False, ""
        room_id = bundle.registry.room_for(chat_id)
        if bundle.rooms is None or not room_id or not base:
            return True, ""
        renamed = self._gone_name(base, reason)
        try:
            ok = await bundle.rooms.set_name(room_id, renamed)
        except Exception:  # noqa: BLE001 - the mark matters more than the label
            log.exception("could not rename %s for gone chat %s", room_id, chat_id)
            return True, ""
        return True, (renamed if ok is not False else "")

    async def _cmd_check(
        self,
        bundle: AccountBundle,
        query: str,
        room: str = "",
        default_chat: Optional[int] = None,
    ) -> None:
        """Find chats that no longer exist on Telegram and flag their rooms.

        The room and its history are kept — the bridge exists to preserve the
        record — but its name says the other side is gone, so a room that will
        never receive another message is not mistaken for a quiet one.
        """
        if bundle.rooms is None:
            await self._say(room, note(
                f"「{bundle.account.label}」还没绑定空间，没有专属房间可检查。"
            ))
            return
        target = query.strip()
        if not target and default_chat is not None:
            target = str(default_chat)

        items = bundle.registry.items()
        if target and target.lower() not in ("all", "全部"):
            dialog = await self._resolve(bundle, target)
            wanted = dialog.id if dialog else (
                int(target) if target.lstrip("-").isdigit() else None
            )
            if wanted is None:
                await self._say(room, note(f"找不到目标：{target}"))
                return
            items = [i for i in items if i[0] == wanted]
            if not items:
                await self._say(room, note(f"「{target}」没有专属房间。"))
                return
        if not items:
            await self._say(room, note("还没有专属房间。"))
            return

        await self._say(room, note(f"正在检查 {len(items)} 个对话是否还存在…"))
        marked, restored, alive = [], [], 0
        for chat_id, room_id, name in items:
            try:
                status = await bundle.directory.presence(chat_id)
            except Exception:  # noqa: BLE001 - never fail the whole sweep
                log.exception("presence check failed for %s", chat_id)
                continue
            gone = status in ("gone", "deleted")
            if gone and (await self._mark_gone(bundle, chat_id, status, name))[0]:
                marked.append(f"{name}（{_GONE_REASON[status]}）")
            elif not gone and bundle.registry.set_deleted(chat_id, False):
                await bundle.rooms.set_name(room_id, name)
                restored.append(name)
            elif not gone:
                alive += 1

        lines = [f"✅ 正常 {alive} 个"]
        if marked:
            lines += ["", "🗑 已标记为删除："] + [f"  {m}" for m in marked]
        if restored:
            lines += ["", "♻️ 恢复正常（已还原房间名）："] + [f"  {r}" for r in restored]
        if not marked and not restored:
            lines.append("没有发现已删除的对话。")
        await self._say(room, panel(f"对话状态检查 · 共 {len(items)} 个", lines))

    async def _cmd_avatar(
        self,
        bundle: AccountBundle,
        query: str,
        room: str = "",
        default_chat: Optional[int] = None,
    ) -> None:
        """Mirror Telegram profile photos onto the dedicated rooms.

        Avatars are set when a room is created; this re-syncs them after the
        other side changes their photo, and backfills rooms made before the
        feature existed.
        """
        if bundle.rooms is None:
            await self._say(room, note(
                f"「{bundle.account.label}」还没绑定空间，没有专属房间可设头像。"
            ))
            return
        target = query.strip()
        if not target and default_chat is not None:
            target = str(default_chat)

        if target.lower() in ("all", "全部"):
            items = bundle.registry.items()
            if not items:
                await self._say(room, note("还没有专属房间。"))
                return
            await self._say(room, note(f"正在同步 {len(items)} 个房间的头像…"))
            tally: dict[str, list[str]] = {}
            for chat_id, room_id, name in items:
                try:
                    outcome = await bundle.rooms.set_avatar(room_id, chat_id)
                except Exception:  # noqa: BLE001 - cosmetic, keep going
                    log.exception("avatar sync failed for %s", chat_id)
                    outcome = "error"
                tally.setdefault(outcome, []).append(name or str(chat_id))
            lines = [
                f"✅ 已更新 {len(tally.get('set', []))} 个",
                f"⏭ 已是最新 {len(tally.get('unchanged', []))} 个",
            ]
            # Name the ones that got nothing: "4/6 更新" leaves you guessing
            # which two are missing and why.
            for key, label in (("none", "对方没有头像/无权查看"),
                               ("error", "失败（见日志）")):
                if tally.get(key):
                    lines.append("")
                    lines.append(f"{label}：")
                    lines.extend(f"  {n}" for n in tally[key])
            await self._say(room, panel(f"头像同步 · 共 {len(items)} 个房间", lines))
            return

        if not target:
            await self._say(room, note(
                f"用法：{self._prefix} avatar <目标>（或 {self._prefix} avatar all "
                f"同步全部；在专属房间里直接发 {self._prefix} avatar）"
            ))
            return

        dialog = await self._resolve(bundle, target)
        chat_id = dialog.id if dialog else (
            default_chat if default_chat is not None else None
        )
        if chat_id is None:
            await self._say(room, note(f"找不到目标：{target}"))
            return
        room_id = bundle.registry.room_for(chat_id)
        if not room_id:
            await self._say(room, note(
                f"「{dialog.name if dialog else chat_id}」还没有专属房间，"
                f"用 {self._prefix} room <目标> 创建。"
            ))
            return
        name = dialog.name if dialog else bundle.registry.name_for(chat_id)
        outcome = await bundle.rooms.set_avatar(room_id, chat_id)
        await self._say(room, note({
            "set": f"🖼 已同步「{name}」的头像。",
            "unchanged": f"「{name}」的头像和当前房间头像一致，无需更新。",
            "none": f"「{name}」没有可获取的头像"
                    f"（对方没设置，或隐私设置不允许本账户查看）。",
        }.get(outcome, f"同步「{name}」的头像失败，见日志。")))

    async def _cmd_rooms(self, bundle: AccountBundle, room: str = "") -> None:
        items = bundle.registry.items()
        if not items:
            await self._say(room, note(
                f"「{bundle.account.label}」还没有专属房间。有新消息时会自动创建，"
                f"或用 {self._prefix} room <目标> 手动创建。"
            ))
            return
        lines = [f"{name or chat_id} — {room}" for chat_id, room, name in items]
        lines.append("")
        lines.append(f"共 {len(items)} 个")
        await self._say(room, panel(f"专属房间 · {bundle.account.label}", lines))

    async def _cmd_dms(self, bundle: AccountBundle, count: str, room: str = "") -> None:
        try:
            rows = await bundle.directory.list_dms()
        except Exception:  # noqa: BLE001
            log.exception("list_dms failed")
            await self._say(room, note("获取私信列表失败，见日志。"))
            return
        if not rows:
            await self._say(room, note("没有任何私信对话。"))
            return
        try:
            limit = max(1, min(int(count), 100)) if count else 30
        except ValueError:
            limit = 30

        active = self._active_target(bundle)
        lines: list[str] = []
        for s in rows[:limit]:
            d = s.dialog
            marks = ""
            if active and active in (str(d.id), d.username):
                marks += " ⭐"
            if bundle.state.is_muted(str(d.id)):
                marks += " 🔕"
            unread = f" 🔴{s.unread}" if s.unread else ""
            uname = f" @{d.username}" if d.username else ""
            lines.append(f"{d.name}{uname}{unread}{marks} — {d.id}")
            preview = self._preview(s)
            if preview:
                lines.append(f"    {preview}")
        if len(rows) > limit:
            lines.append("")
            lines.append(f"（共 {len(rows)} 个，只显示前 {limit} 个）")
        lines.append("")
        lines.append(f"看内容：{self._prefix} dm <目标> [条数]")
        await self._say(room, panel(f"私信 ({len(rows)})", lines))

    def _preview(self, s) -> str:
        """One-line summary of a dialog's last message: when, who, what."""
        body = " ".join((s.last_text or "").split())
        if not body and s.last_media:
            body = "[媒体]"
        if not body:
            return ""
        if len(body) > 60:
            body = body[:60] + "…"
        when = ""
        if s.last_date:
            when = datetime.fromtimestamp(s.last_date, self._tz).strftime("%m-%d %H:%M")
        who = "我: " if s.last_outgoing else ""
        return f"{when} {who}{body}".strip()

    async def _cmd_dm(self, bundle: AccountBundle, query: str, count: str, room: str = "") -> None:
        if not query:
            await self._say(room, note(
                f"用法：{self._prefix} dm <目标> [条数]（用 {self._prefix} dms 看全部）"
            ))
            return
        try:
            limit = max(1, min(int(count), 50)) if count else 20
        except ValueError:
            limit = 20
        # Restricted to private chats: a group sharing a contact's name must
        # not win a lookup the user explicitly scoped to DMs.
        dialog = await self._resolve(bundle, query, kind="user")
        if dialog is None:
            await self._say(room, note(
                f"找不到私信对象：{query}。用 {self._prefix} dms 查看全部私信。"
            ))
            return
        try:
            rows = await bundle.directory.history(str(dialog.id), limit)
        except Exception:  # noqa: BLE001
            log.exception("history failed")
            await self._say(room, note("获取私信内容失败，见日志。"))
            return
        if not rows:
            await self._say(room, note(f"{dialog.name} 没有可显示的消息。"))
            return
        uname = f" @{dialog.username}" if dialog.username else ""
        await self._say(room, panel(
            f"私信 · {dialog.name}{uname} · 最近 {len(rows)} 条",
            [f"{s}: {b}" for s, b in rows],
        ))

    async def _cmd_use(self, bundle: AccountBundle, query: str, room: str = "") -> None:
        if not query:
            await self._say(room, note(f"用法：{self._prefix} use <目标>"))
            return
        dialog = await self._resolve(bundle, query)
        if dialog is None:
            await self._say(room, note(f"找不到目标：{query}"))
            return
        bundle.state.set_active_target(_ACTIVE_TARGET, str(dialog.id))
        await self._say(room, note(
            f"✅ 「{bundle.account.label}」的当前发送目标：{dialog.name}"
        ))

    async def _cmd_who(self, bundle: AccountBundle, room: str = "") -> None:
        active = self._active_target(bundle)
        if not active:
            await self._say(room, note("当前没有发送目标。"))
            return
        dialog = await self._resolve(bundle, active)
        await self._say(room, note(
            f"「{bundle.account.label}」当前发送目标：{dialog.name if dialog else active}"
        ))

    async def _cmd_read(self, bundle: AccountBundle, query: str, count: str, room: str = "") -> None:
        if not query:
            await self._say(room, note(f"用法：{self._prefix} read <目标> [条数]"))
            return
        try:
            limit = max(1, min(int(count), 50)) if count else 10
        except ValueError:
            limit = 10
        dialog = await self._resolve(bundle, query)
        if dialog is None:
            await self._say(room, note(f"找不到目标：{query}"))
            return
        try:
            rows = await bundle.directory.history(str(dialog.id), limit)
        except Exception:  # noqa: BLE001
            log.exception("history failed")
            await self._say(room, note("获取历史消息失败，见日志。"))
            return
        if not rows:
            await self._say(room, note(f"{dialog.name} 没有可显示的消息。"))
            return
        lines = [f"{s}: {b}" for s, b in rows]
        await self._say(room, panel(f"{dialog.name} · 最近 {len(rows)} 条", lines))

    async def _cmd_stats(self, bundle: AccountBundle, room: str = "") -> None:
        await self._say(room, note("正在统计你的消息记录…（对话多时稍等）"))
        try:
            rows = await bundle.directory.own_message_stats()
        except Exception:  # noqa: BLE001
            log.exception("own_message_stats failed")
            await self._say(room, note("统计失败，见日志。"))
            return
        if not rows:
            await self._say(room, note("没有找到你发过消息的对话。"))
            return
        total = sum(c for _, c in rows)
        lines = [
            f"{_KIND_ICON.get(d.kind, '•')} {d.name} — {c} 条  ({d.id})"
            for d, c in rows
        ]
        lines.append("")
        lines.append(f"共 {len(rows)} 个对话，{total} 条消息")
        await self._say(room, panel(f"消息记录 · {bundle.account.label}", lines))

    async def _cmd_settings(self, bundle: AccountBundle, room: str) -> None:
        active = self._active_target(bundle)
        active_name = active or "未设置"
        if active:
            dialog = await self._resolve(bundle, active)
            if dialog is not None:
                active_name = f"{dialog.name} ({dialog.id})"
        f = bundle.state.delay_fixed()
        r = bundle.state.delay_random()
        delay_line = (
            "关闭" if f == 0 and r == 0
            else f"固定 {format_duration(f)} + 随机 0~{format_duration(r)}"
        )
        sd = bundle.state.self_destruct_all()
        mode = bundle.state.forward_mode()
        accounts = self._accounts.accounts()
        lines = [
            f"Telegram 账户：{len(accounts)} 个在线",
            f"当前账户：{bundle.account.label if bundle else '（无）'}",
            f"当前发送目标：{active_name}",
            f"发送模式：{_FMSG_TITLE.get(mode, mode)}",
            f"发送延迟：{delay_line}",
            "自毁：",
            "  " + " · ".join(
                f"{_KIND_TITLE[k]} {format_duration(sd.get(k, 0))}" for k in _KINDS
            ),
            f"接收白名单：{len(bundle.state.watched())} 个群/频道/机器人"
            f"（真人私信默认转发；机器人必须 watch）",
            f"静音：{len(bundle.state.muted())} 个",
            f"时区：{self._tz_name}",
            f"命令前缀：{self._prefix}",
        ]
        await self._reply_in(room, panel("当前设置", lines))

    async def _cmd_mute(self, bundle: AccountBundle, query: str, mute: bool, room: str = "") -> None:
        if not query:
            verb = "mute" if mute else "unmute"
            await self._say(room, note(f"用法：{self._prefix} {verb} <目标>"))
            return
        dialog = await self._resolve(bundle, query)
        if dialog is None:
            await self._say(room, note(f"找不到目标：{query}"))
            return
        if mute:
            bundle.state.mute(str(dialog.id))
            await self._say(room, note(f"🔕 已静音：{dialog.name}（仍显示，但不提醒）"))
        else:
            bundle.state.unmute(str(dialog.id))
            await self._say(room, note(f"🔔 已取消静音：{dialog.name}"))

    async def _cmd_muted(self, bundle: AccountBundle, room: str = "") -> None:
        ids = bundle.state.muted()
        if not ids:
            await self._say(room, note("没有已静音的对话。"))
            return
        await self._say(room, panel("已静音", sorted(ids)))

    async def _cmd_watch(self, bundle: AccountBundle, query: str, watch: bool, room: str = "") -> None:
        if not query:
            verb = "watch" if watch else "unwatch"
            await self._say(room, note(f"用法：{self._prefix} {verb} <目标>"))
            return
        dialog = await self._resolve(bundle, query)
        if dialog is None:
            await self._say(room, note(f"找不到目标：{query}"))
            return
        if dialog.kind == "user":
            await self._say(room, note(f"{dialog.name} 是私信，默认就转发，无需 watch。"))
            return
        if watch:
            bundle.state.watch(str(dialog.id))
            await self._say(room, note(f"👁 已加入接收白名单：{dialog.name}"))
            return
        bundle.state.unwatch(str(dialog.id))
        if dialog.kind == "bot":
            # For a bot the allow-list is the whole story, so unwatch really
            # does silence it — even though its room stays.
            await self._say(room, note(
                f"已移出接收白名单：{dialog.name}（机器人不再转发，"
                f"专属房间保留但不会再有新消息）"
            ))
            return
        # A dedicated room relays on its own, so unwatch alone won't silence it
        # — saying so beats leaving the user wondering why messages keep coming.
        if bundle.registry.room_for(dialog.id):
            await self._say(room, note(
                f"已移出接收白名单：{dialog.name}。但它有专属房间，仍会继续转发到那里"
                f"（可用 {self._prefix} mute {dialog.id} 只收不提醒）。"
            ))
        else:
            await self._say(room, note(f"已移出接收白名单：{dialog.name}"))

    def _relayed(
        self, bundle: AccountBundle, chat_id: int | str, kind: str = ""
    ) -> bool:
        """Whether this chat's messages reach Matrix — the same rule `Relay`
        applies, and it has to stay in step with `Relay._should_relay`."""
        if kind == "user":
            return True  # a person's DM always relays
        if bundle.state.is_watched(str(chat_id)):
            return True
        if kind == "bot":
            return False  # bots relay on the allow-list alone, room or not
        return bool(bundle.registry.room_for(chat_id))

    async def _cmd_watching(self, bundle: AccountBundle, room: str = "") -> None:
        ids = bundle.state.watched()
        # Dedicated rooms relay without being watched; listing only the watch
        # list would under-report what actually arrives.
        roomed = [
            (str(cid), name)
            for cid, _room, name in bundle.registry.items()
            # Negative ids are groups/channels; DMs relay either way, and a
            # bot's room does NOT relay on its own, so neither belongs here.
            if str(cid).startswith("-") and str(cid) not in ids
        ]
        if not ids and not roomed:
            await self._say(room, note(
                f"接收白名单为空（当前只转发真人私信）。"
                f"用 {self._prefix} watch <群/频道/机器人> 添加。"
            ))
            return
        lines = sorted(ids)
        if roomed:
            lines.append("")
            lines.append("以下有专属房间，同样会转发：")
            lines.extend(f"{name or cid} — {cid}" for cid, name in sorted(roomed))
        await self._say(room, panel("接收白名单（群/频道/机器人）", lines))

    async def _cmd_selfdestruct(self, bundle: AccountBundle, type_arg: str,
                                dur_arg: str, room: str) -> None:
        if not type_arg:
            cur = bundle.state.self_destruct_all()
            lines = [
                f"{_KIND_ICON[k]} {_KIND_TITLE[k]}：{format_duration(cur.get(k, 0))}"
                for k in _KINDS
            ]
            lines += ["", f"设置：{self._prefix} selfdestruct 私信 1h（0=关闭）"]
            await self._reply_in(room, panel("自毁设置（转发到 TG 的消息）", lines))
            return
        kind = _KIND_ALIASES.get(type_arg.lower())
        if kind is None:
            await self._reply_in(room, note(
                "类型只能是 私信 / 机器人 / 群组 / 频道"
                "（或 dm/bot/group/channel）。"
            ))
            return
        secs = parse_duration(dur_arg)
        if secs is None:
            await self._reply_in(room, note("时长格式无效。例：1d2h30m、45s、0（关闭）。"))
            return
        bundle.state.set_self_destruct(kind, secs)
        if secs == 0:
            await self._reply_in(room, note(f"已关闭「{_KIND_TITLE[kind]}」的自毁。"))
        else:
            await self._reply_in(room, note(
                f"⏱ 「{_KIND_TITLE[kind]}」自毁时间设为 {format_duration(secs)}。"
                f"（对之后转发的消息生效）"
            ))

    async def _cmd_delmsg(
        self,
        bundle: AccountBundle,
        scope: str,
        confirm: str,
        room: Optional[str] = None,
        default_chat: Optional[int] = None,
    ) -> None:
        """Delete your own Telegram messages in a scope.

        In a dedicated room the scope is implicit — it is that room's chat — so
        `delMsg` / `delMsg confirm` is all that has to be typed there.
        """
        room = room or self._room
        if default_chat is not None:
            scope = str(default_chat)  # the room's own chat, never anything else

        if not scope:
            await self._reply_in(room, note(
                f"用法：{self._prefix} delMsg <目标|AllUser|AllGroup|AllChannel|AllChat> confirm"
            ))
            return
        bulk = _BULK_SCOPES.get(scope.lower())
        if bulk is not None:
            try:
                dialogs = await bundle.directory.list_dialogs()
            except Exception:  # noqa: BLE001
                log.exception("list_dialogs failed")
                await self._reply_in(room, note("获取对话列表失败，见日志。"))
                return
            targets = [d for d in dialogs if d.kind in bulk]
            label = scope
        else:
            dialog = await self._resolve(bundle, scope)
            if dialog is None and default_chat is not None:
                # The room knows its chat even when the lookup can't name it,
                # so an unresolvable dialog must not block an in-room delete.
                name = bundle.registry.name_for(default_chat) or str(default_chat)
                dialog = Dialog(id=default_chat, name=name, kind="user")
            if dialog is None:
                await self._reply_in(room, note(f"找不到目标：{scope}"))
                return
            targets = [dialog]
            label = dialog.name

        if confirm.lower() != "confirm":
            how = (f"{self._prefix} delMsg confirm" if default_chat is not None
                   else f"{self._prefix} delMsg {scope} confirm")
            await self._reply_in(room, note(
                f"⚠️ 将删除「{label}」范围内（{len(targets)} 个对话）你自己的所有消息，"
                f"不可恢复。确认请发：{how}"
            ))
            return

        await self._reply_in(
            room, note(f"正在删除「{label}」内你的消息（{len(targets)} 个对话）…")
        )
        total = 0
        failed = 0
        for d in targets:
            try:
                total += await bundle.directory.delete_own_messages(d.id)
            except Exception:  # noqa: BLE001
                failed += 1
                log.warning("delete_own_messages failed for %s", d.id)
        result = f"✅ 已删除 {total} 条消息。"
        if failed:
            result += f"（{failed} 个对话失败，可能无权限）"
        await self._reply_in(room, note(result))

    # -- reply helper --------------------------------------------------------

    async def _reply(self, html: str) -> None:
        await self._reply_in(self._room, html)

    async def _say(self, room: str, html: str) -> None:
        """Answer where the command was typed, falling back to the root room."""
        await self._reply_in(room or self._room, html)

    async def _reply_in(self, room: str, html: str) -> None:
        await self._mx.deliver(
            Target(chat_id=room),
            OutboundMessage(kind=MessageKind.TEXT, text=html, html=True, silent=True),
        )
