import argparse
import io

import pytest

from bridge import mxlogin


def _args(**over):
    base = dict(password_stdin=False, token=False, token_stdin=False, secret="")
    base.update(over)
    return argparse.Namespace(**base)


def test_stdin_secret_is_consumed_before_prompts(monkeypatch):
    """The piped line must be the secret, not an answer to the first prompt.

    input() and the secret share one stdin; reading the secret late let a piped
    token get swallowed by the homeserver question.
    """
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret\nleftover\n"))
    args = _args(token_stdin=True)
    mxlogin._consume_stdin_secret(args)
    assert args.secret == "s3cret"


def test_no_stdin_flag_leaves_secret_empty(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not-a-secret\n"))
    args = _args()
    mxlogin._consume_stdin_secret(args)
    assert args.secret == ""


def test_empty_stdin_is_an_error(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(SystemExit):
        mxlogin._consume_stdin_secret(_args(password_stdin=True))


def test_trailing_carriage_return_is_stripped(monkeypatch):
    """A token piped from a CRLF file must not carry \\r into the header."""
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret\r\n"))
    args = _args(token_stdin=True)
    mxlogin._consume_stdin_secret(args)
    assert args.secret == "s3cret"


def test_read_secret_uses_the_stashed_value():
    got = mxlogin._read_secret(True, "access token", "--token-stdin", "abc")
    assert got == "abc"


def test_read_secret_never_prompts_when_piped(monkeypatch):
    monkeypatch.setattr(
        mxlogin.getpass, "getpass", lambda *_: pytest.fail("must not prompt")
    )
    assert mxlogin._read_secret(True, "password", "--password-stdin", "pw") == "pw"


@pytest.mark.parametrize(
    "over,expected",
    [
        (dict(token=True), "token"),
        (dict(token_stdin=True), "token"),
        (dict(password_stdin=True), "password"),
    ],
)
def test_method_from_flags_skips_the_menu(over, expected, monkeypatch):
    monkeypatch.setattr(mxlogin, "_ask", lambda *a, **k: pytest.fail("no menu"))
    assert mxlogin._choose_method(_args(**over)) == expected


@pytest.mark.parametrize(
    "choice,expected",
    [("1", "password"), ("", "password"), ("2", "token"), ("token", "token")],
)
def test_method_menu_choices(choice, expected, monkeypatch):
    monkeypatch.setattr(mxlogin, "_ask", lambda *a, **k: choice or "1")
    assert mxlogin._choose_method(_args()) == expected


def test_ask_falls_back_to_default_without_a_tty(monkeypatch):
    """A configured default is a real answer - do not fail on a solved question."""
    monkeypatch.setattr("builtins.input", lambda *_: (_ for _ in ()).throw(EOFError))
    assert mxlogin._ask("homeserver", "https://matrix.org") == "https://matrix.org"


def test_ask_without_default_and_without_tty_exits(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: (_ for _ in ()).throw(EOFError))
    with pytest.raises(SystemExit):
        mxlogin._ask("control room")


def test_banner_is_ascii_only():
    """cp936 / legacy Windows consoles mangle anything above 7-bit."""
    mxlogin.BANNER.encode("ascii")
