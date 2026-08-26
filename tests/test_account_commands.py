"""`!tg accounts / login / code / 2fa / switch / bind / logout`.

The security-relevant behaviours: the phone code and 2FA password are redacted
the moment they are seen, and never echoed back into the room.
"""

import pytest

from bridge.accounts import TelegramAccount
from bridge.core.dispatcher import Dispatcher, parse_options
from bridge.core.models import AccountResult, InboundMessage, MessageKind
from bridge.core.roomregistry import RoomRegistry
from bridge.core.state import BridgeState

from .fakes import FakeAccounts, FakeDirectory, FakeSender, FakeSpaces, RecordingSink

CONTROL = "!control:matrix.org"
SPACE_ROOM = "!inside-space:matrix.org"
SPACE = "!thespace:matrix.org"
CODE = "54321"


class FakeRedactor:
    def __init__(self, fail=False):
        self.redacted: list[tuple[str, str]] = []
        self.fail = fail

    async def redact(self, room_id: str, event_id: str) -> None:
        if self.fail:
            raise RuntimeError("no permission")
        self.redacted.append((room_id, event_id))


def _build(accounts=None, spaces=None, redactor=None):
    mx = RecordingSink()
    accounts = accounts if accounts is not None else FakeAccounts()
    redactor = redactor if redactor is not None else FakeRedactor()
    spaces = spaces if spaces is not None else FakeSpaces({SPACE_ROOM: SPACE})
    d = Dispatcher(
        accounts, mx, BridgeState(), CONTROL,
        command_prefix="!tg", timezone="UTC",
        redactor=redactor, spaces=spaces,
    )
    return d, mx, accounts, redactor


def _cmd(text, room=CONTROL, event="$cmd1"):
    return InboundMessage(kind=MessageKind.TEXT, source_room=room, sender="@me",
                          text=text, event_id=event)


def _texts(mx):
    return "\n".join(m.text or "" for _t, m in mx.deliveries)


# -- option parsing ------------------------------------------------------------


def test_parse_options_splits_args_and_keys():
    args, opts = parse_options("!tg login +8613800138000 space=!s:hs", "!tg")
    assert args == ["+8613800138000"] and opts == {"space": "!s:hs"}


def test_parse_options_order_does_not_matter():
    args, opts = parse_options("!tg bind space=!s:hs alice", "!tg")
    assert args == ["alice"] and opts["space"] == "!s:hs"


# -- accounts listing ----------------------------------------------------------


async def test_accounts_lists_telegram_name_and_id():
    d, mx, _a, _r = _build()
    await d.on_matrix_message(_cmd("!tg accounts"))
    body = _texts(mx)
    assert "Me (@me)" in body and "1001" in body
    assert "⭐当前" in body


async def test_accounts_warns_when_no_space_is_bound():
    d, mx, _a, _r = _build()
    await d.on_matrix_message(_cmd("!tg accounts"))
    assert "未绑定空间" in _texts(mx)


async def test_accounts_shows_the_bound_space():
    accounts = FakeAccounts(account=TelegramAccount(
        tg_id=7, name="Bound", username="b", space_id=SPACE))
    d, mx, _a, _r = _build(accounts)
    await d.on_matrix_message(_cmd("!tg accounts"))
    assert SPACE in _texts(mx)


async def test_accounts_empty_points_at_login():
    accounts = FakeAccounts()
    accounts.accounts = lambda: []
    d, mx, _a, _r = _build(accounts)
    await d.on_matrix_message(_cmd("!tg accounts"))
    assert "login" in _texts(mx)


# -- login ---------------------------------------------------------------------


async def test_login_requests_a_code():
    d, mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg login +8613800138000"))
    assert accounts.logins == [("+8613800138000", "", "")]
    assert "验证码" in _texts(mx)


async def test_login_in_a_space_room_binds_that_space():
    """The whole point of allowing the command outside the control room."""
    d, _mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg login +8613800138000", room=SPACE_ROOM))
    assert accounts.logins == [("+8613800138000", SPACE, SPACE_ROOM)]


async def test_explicit_space_option_wins():
    d, _mx, accounts, _r = _build()
    await d.on_matrix_message(
        _cmd("!tg login +8613800138000 space=!other:hs", room=SPACE_ROOM)
    )
    assert accounts.logins[0][1] == "!other:hs"


async def test_login_from_the_control_room_binds_nothing():
    d, _mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg login +8613800138000"))
    assert accounts.logins[0][1] == ""


async def test_login_without_a_phone_shows_usage():
    d, mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg login"))
    assert accounts.logins == []
    assert "手机号" in _texts(mx)


async def test_login_rejects_a_non_number():
    d, mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg login not-a-phone"))
    assert accounts.logins == []
    assert "格式" in _texts(mx)


async def test_login_failure_is_reported():
    accounts = FakeAccounts()
    accounts.result = AccountResult(ok=False, error="FloodWaitError: 60")
    d, mx, _a, _r = _build(accounts)
    await d.on_matrix_message(_cmd("!tg login +8613800138000"))
    assert "FloodWaitError" in _texts(mx)


# -- the code and 2FA steps ----------------------------------------------------


async def test_code_is_redacted_before_anything_else():
    d, _mx, accounts, redactor = _build()
    await d.on_matrix_message(_cmd(f"!tg code {CODE}"))
    assert redactor.redacted == [(CONTROL, "$cmd1")]
    assert accounts.codes == [CODE]


async def test_code_is_never_echoed_back():
    d, mx, _a, _r = _build()
    await d.on_matrix_message(_cmd(f"!tg code {CODE}"))
    assert CODE not in _texts(mx)


async def test_2fa_password_is_redacted_and_not_echoed():
    accounts = FakeAccounts()
    accounts.stage = "password"
    d, mx, _a, redactor = _build(accounts)
    await d.on_matrix_message(_cmd("!tg 2fa hunter2"))
    assert redactor.redacted and accounts.passwords == ["hunter2"]
    assert "hunter2" not in _texts(mx)


async def test_code_without_a_pending_login_says_so():
    accounts = FakeAccounts()
    accounts.stage = ""
    d, mx, _a, redactor = _build(accounts)
    await d.on_matrix_message(_cmd(f"!tg code {CODE}"))
    assert accounts.codes == []
    assert "没有正在进行的登录" in _texts(mx)
    assert redactor.redacted  # it was still a secret in a room


async def test_two_factor_prompt_follows_the_code():
    accounts = FakeAccounts()
    accounts.result = AccountResult(ok=True, stage="password")
    d, mx, _a, _r = _build(accounts)
    await d.on_matrix_message(_cmd(f"!tg code {CODE}"))
    assert "两步验证" in _texts(mx)


async def test_successful_login_reports_the_account():
    d, mx, _a, _r = _build()
    await d.on_matrix_message(_cmd(f"!tg code {CODE}"))
    assert "登录成功" in _texts(mx) or "Me (@me)" in _texts(mx)


async def test_redaction_failure_warns_but_still_proceeds():
    d, mx, accounts, _r = _build(redactor=FakeRedactor(fail=True))
    await d.on_matrix_message(_cmd(f"!tg code {CODE}"))
    assert "手动删除" in _texts(mx)
    assert accounts.codes == [CODE]


async def test_a_secret_command_in_a_chat_room_is_redacted_not_forwarded():
    """A code typed into a room shared with a real person must not be sent."""
    reg = RoomRegistry()
    reg.register(111, "!r1:hs", "Alice")
    sender = FakeSender()
    accounts = FakeAccounts(FakeDirectory(), sender, registry=reg)
    d, mx, _a, redactor = _build(accounts)

    await d.on_matrix_message(_cmd(f"!tg code {CODE}", room="!r1:hs"))

    assert sender.submissions == []
    assert redactor.redacted == [("!r1:hs", "$cmd1")]
    assert CODE not in _texts(mx)


# -- switch / bind / logout ----------------------------------------------------


async def test_switch_by_position():
    d, _mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg switch 2"))
    assert accounts.switched == ["2"]


async def test_switch_says_other_accounts_stay_online():
    """Unlike a Matrix account switch, nothing goes offline."""
    d, mx, _a, _r = _build()
    await d.on_matrix_message(_cmd("!tg switch 2"))
    assert "仍然在线" in _texts(mx)


async def test_switch_without_a_target_shows_usage():
    d, _mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg switch"))
    assert accounts.switched == []


async def test_bind_uses_the_space_of_the_room_it_was_typed_in():
    d, _mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg bind", room=SPACE_ROOM))
    assert accounts.bound == [("", SPACE)]


async def test_bind_names_an_account_explicitly():
    d, _mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg bind 1001", room=SPACE_ROOM))
    assert accounts.bound == [("1001", SPACE)]


async def test_bind_outside_any_space_explains():
    d, mx, accounts, _r = _build(spaces=FakeSpaces({}))
    await d.on_matrix_message(_cmd("!tg bind", room="!nowhere:hs"))
    assert accounts.bound == []
    assert "不在任何空间" in _texts(mx)


async def test_unbind_clears_the_space():
    d, _mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg unbind"))
    assert accounts.bound == [("", "")]


async def test_logout_requires_confirmation():
    d, mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg logout 1001"))
    assert accounts.logged_out == []
    assert "confirm" in _texts(mx)


async def test_logout_prompt_points_at_switch_for_the_harmless_case():
    d, mx, _a, _r = _build()
    await d.on_matrix_message(_cmd("!tg logout 1001"))
    assert "switch" in _texts(mx)


async def test_logout_confirm_removes_the_account():
    d, mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg logout 1001 confirm"))
    assert accounts.logged_out == ["1001"]
    assert "已退出" in _texts(mx)


async def test_bare_logout_confirm_means_the_current_account():
    """Not "the account named confirm", which would never resolve."""
    d, _mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg logout confirm"))
    assert accounts.logged_out == [""]


async def test_logout_says_server_side_copies_remain():
    d, mx, _a, _r = _build()
    await d.on_matrix_message(_cmd("!tg logout 1001 confirm"))
    assert "仍在服务器上" in _texts(mx)


# -- per-space control rooms ---------------------------------------------------


def _bound(tg_id=2002, name="Second", control="!ctl2:matrix.org", space=SPACE):
    return TelegramAccount(tg_id=tg_id, name=name, username="second",
                           space_id=space, control_room=control)


async def test_login_makes_that_room_the_accounts_control_room():
    """"登录时所在的房间" is the natural place to then drive the account from."""
    d, _mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg login +8613800138000", room=SPACE_ROOM))
    _phone, _space, control = accounts.logins[0]
    assert control == SPACE_ROOM


async def test_login_in_the_root_room_claims_no_control_room():
    d, _mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg login +8613800138000"))
    assert accounts.logins[0][2] == ""


async def test_control_binds_this_room_to_an_account():
    d, _mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg control 1001", room=SPACE_ROOM))
    assert accounts.controls == [("1001", SPACE_ROOM)]


async def test_control_defaults_to_the_current_account():
    d, _mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg control", room=SPACE_ROOM))
    assert accounts.controls == [("", SPACE_ROOM)]


async def test_control_show_lists_the_rooms():
    accounts = FakeAccounts(account=_bound())
    d, mx, _a, _r = _build(accounts)
    await d.on_matrix_message(_cmd("!tg control show"))
    assert "!ctl2:matrix.org" in _texts(mx)


async def test_an_accounts_own_control_room_drives_that_account():
    accounts = FakeAccounts(account=_bound())
    d, mx, _a, _r = _build(accounts)
    await d.on_matrix_message(_cmd("!tg accounts", room="!ctl2:matrix.org"))
    assert "Second" in _texts(mx)


async def test_commands_work_in_an_accounts_own_control_room():
    """Not just account management — the full command set lives there."""
    accounts = FakeAccounts(FakeDirectory(), FakeSender(), account=_bound())
    d, mx, _a, _r = _build(accounts)
    await d.on_matrix_message(_cmd("!tg who", room="!ctl2:matrix.org"))
    assert "全局房间" not in _texts(mx)  # it was accepted, not deflected


async def test_settings_are_per_account():
    """A TTL set for one account must not reach into another's."""
    from bridge.core.state import BridgeState

    own = BridgeState()
    accounts = FakeAccounts(account=_bound(), state=own)
    d, _mx, _a, _r = _build(accounts)

    await d.on_matrix_message(_cmd("!tg selfdestruct 群组 1h", room="!ctl2:matrix.org"))

    assert own.self_destruct("group") == 3600


async def test_settings_replies_land_in_that_control_room():
    accounts = FakeAccounts(account=_bound())
    d, mx, _a, _r = _build(accounts)
    await d.on_matrix_message(_cmd("!tg delay 5s", room="!ctl2:matrix.org"))
    assert mx.deliveries[-1][0].chat_id == "!ctl2:matrix.org"


# -- routing -------------------------------------------------------------------


async def test_account_commands_work_in_a_room_outside_the_bridge():
    """That is what lets an account be bound from inside its own Space."""
    d, mx, accounts, _r = _build()
    await d.on_matrix_message(_cmd("!tg accounts", room=SPACE_ROOM))
    assert "Me (@me)" in _texts(mx)


async def test_other_commands_are_refused_outside_the_control_room():
    d, mx, _a, _r = _build()
    await d.on_matrix_message(_cmd("!tg list", room=SPACE_ROOM))
    assert "控制房间" in _texts(mx)


async def test_plain_text_in_an_unknown_room_is_ignored():
    sender = FakeSender()
    accounts = FakeAccounts(FakeDirectory(), sender)
    d, mx, _a, _r = _build(accounts)
    await d.on_matrix_message(_cmd("just chatting", room="!random:hs"))
    assert sender.submissions == [] and mx.deliveries == []


async def test_commands_needing_an_account_say_so_when_there_is_none():
    accounts = FakeAccounts()
    accounts.current = lambda: None
    d, mx, _a, _r = _build(accounts)
    await d.on_matrix_message(_cmd("!tg list"))
    assert "还没有登录任何 Telegram 账户" in _texts(mx)


# -- an account whose session died must stay manageable ----------------------


def _offline(tg_id=2002, name="Bravo"):
    from bridge.accounts import TelegramAccount
    return TelegramAccount(tg_id=tg_id, name=name, username="bravo")


async def test_accounts_lists_an_offline_account():
    d, mx, accounts, _ = _build()
    accounts.offline = [_offline()]
    await d.on_matrix_message(_cmd("!tg accounts"))
    body = mx.deliveries[-1][1].text
    assert "Bravo" in body and "离线" in body


async def test_accounts_says_how_to_recover_an_offline_account():
    d, mx, accounts, _ = _build()
    accounts.offline = [_offline()]
    await d.on_matrix_message(_cmd("!tg accounts"))
    body = mx.deliveries[-1][1].text
    assert "tglogin" in body and "保留" in body


async def test_no_offline_notice_when_everything_is_online():
    d, mx, accounts, _ = _build()
    await d.on_matrix_message(_cmd("!tg accounts"))
    assert "离线" not in mx.deliveries[-1][1].text
