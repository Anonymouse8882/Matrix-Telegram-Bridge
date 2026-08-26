"""Interactive Matrix account login — the sibling of `bridge.tglogin`.

Two ways in:

  * **password** (default) - logs in and mints a token for a device the
    *bridge* owns, so logging out of Element cannot kill it;
  * **token** (`--token`) - adopts an access token you paste, for SSO-only
    accounts that have no password to log in with. Verified via /whoami
    before it is stored.

Either way the credentials land on the data volume, so switching account is:

    python -m bridge.mxlogin --config /config/config.yaml
    docker compose restart

Run it where the bridge runs (over SSH for a remote host). Doing the login on
the server rather than on your laptop is deliberate: the homeserver records the
IP and user-agent of whoever calls /login, so the laptop should never make that
call. Everything here goes through the configured proxy, and refuses to run
directly if that proxy cannot be applied.

All output is 7-bit ASCII so it survives cp936/legacy Windows consoles.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from typing import Optional

from nio import (
    AsyncClient,
    AsyncClientConfig,
    JoinResponse,
    LoginResponse,
    LogoutResponse,
    WhoamiResponse,
)

from . import creds as creds_store
from .config import Config, load_config
from .proxy import ProxyError, aiohttp_session, http_proxy_url, parse_proxy
from .purge import purge_matrix_data

BANNER = r"""
   _  _   __   ____  ____  __  _  _
  ( \/ ) /__\ (_  _)(  _ \(  )( \/ )
   )  ( /(__)\  )(   )   / )(  )  (
  (_/\_)__)(__)(__) (_)\_)(__)(_/\_)

   a c c o u n t   l o g i n   ::   tg-bridge
"""

RULE = "-" * 62


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _section(title: str) -> None:
    _say()
    _say(f"[ {title} ]".ljust(62, "-"))


def _ok(msg: str) -> None:
    _say(f"  [ok]   {msg}")


def _warn(msg: str) -> None:
    _say(f"  [warn] {msg}")


def _fail(msg: str) -> None:
    _say(f"  [FAIL] {msg}")


def _ask(prompt: str, default: str = "", required: bool = True) -> str:
    """Prompt with a visible default; empty input keeps the default."""
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            val = input(f"  {prompt}{suffix}: ").strip() or default
        except EOFError:
            # No tty (scripted run). A configured default is a real answer, so
            # use it rather than failing on a question we can already answer.
            if default:
                _say(f"{default}  (no tty - using default)")
                return default
            raise SystemExit(
                f"\n  no tty and no default for {prompt!r} - pass it as a flag"
            )
        if val or not required:
            return val
        _warn("this one is required")


def _confirm(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        val = input(f"  {prompt} ({hint}): ").strip().lower()
    except EOFError:
        return default
    if not val:
        return default
    return val in ("y", "yes")


async def _egress_check(session) -> Optional[str]:
    """Show which address the homeserver will attribute the login to.

    Worth the extra request: it is the difference between believing the proxy
    works and knowing it does, *before* a password crosses the wire.
    """
    try:
        async with session.get(
            "https://api.ipify.org?format=json", timeout=_timeout(15)
        ) as resp:
            data = await resp.json()
            return str(data.get("ip") or "")
    except Exception as exc:  # noqa: BLE001 - diagnostic only, never fatal
        _warn(f"egress check failed ({type(exc).__name__}) - continuing")
        return None


def _timeout(seconds: int):
    import aiohttp

    return aiohttp.ClientTimeout(total=seconds)


def _build_session(cfg: Config):
    """One aiohttp session honouring the proxy, shared by checks and login."""
    import aiohttp

    proxy = parse_proxy(cfg.proxy.url)
    session = aiohttp_session(proxy)
    if session is None:
        session = aiohttp.ClientSession()
    return session, proxy


async def _run(args: argparse.Namespace) -> int:
    _say(BANNER)

    _consume_stdin_secret(args)  # must precede every prompt; see the docstring
    cfg = load_config_lenient(args.config)

    # --- proxy first: nothing else should happen until egress is settled ---
    _section("proxy")
    try:
        session, proxy = _build_session(cfg)
    except ProxyError as exc:
        _fail(str(exc))
        return 2

    async with session:
        if proxy is None:
            _warn(f"no proxy in use (setting: {cfg.proxy.configured!r})")
            _warn("correct behind a full-tunnel VPN, which sets no system")
            _warn("proxy - but WRONG if that tunnel is not actually up.")
        else:
            _ok(f"using {proxy.sanitised()} (from {cfg.proxy.source})")
            if proxy.is_socks and not proxy.rdns:
                _warn("local DNS in use - prefer socks5h:// for remote resolution")

        # Checked before the confirmation below: seeing the actual egress
        # address is the only way to answer "is my tunnel up?" honestly.
        if not args.no_egress_check:
            ip = await _egress_check(session)
            if ip:
                _ok(f"address the homeserver will log: {ip}")

        if proxy is None and not args.yes:
            if not _confirm("is that address the one you want? continue?", False):
                return 1

        # --- account details ---
        _section("account")
        homeserver = args.homeserver or _ask("homeserver", cfg.matrix.homeserver)
        # The account MUST be promptable, not just defaulted: without this line
        # a fresh login silently re-adopts the current account's user id and the
        # only thing you get to change is its password. `--user` still wins for
        # scripted runs; empty input keeps the current account.
        user_id = args.user or _ask("account (@name:server)", cfg.matrix.user_id)
        method = _choose_method(args)

        client = AsyncClient(
            homeserver,
            user_id,
            config=AsyncClientConfig(store_sync_tokens=False),
            proxy=http_proxy_url(proxy),
        )
        client.client_session = session

        try:
            _section("login")
            if method == "token":
                auth = await _auth_with_token(client, args)
            else:
                auth = await _auth_with_password(client, args, cfg)
            if auth is None:
                return 3
            user_id, access_token, device_id = auth
            client.user_id = user_id
            client.access_token = access_token
            _ok(f"authenticated as {user_id}")

            _section("control room")
            control_room = args.room or _ask(
                "control room id/alias", cfg.matrix.control_room
            )
            joined = await _ensure_room(client, control_room)
            if joined is None:
                _fail(f"could not join control room {control_room}")
                _warn("token was still minted; fix the room and re-run")
                return 4
            control_room = joined
            _ok(f"control room joined: {control_room}")

            # --- persist ---
            _section("store")
            _handle_account_switch(cfg, user_id, assume_yes=args.yes)
            await _revoke_superseded(
                cfg, session, proxy, access_token, assume_yes=args.yes
            )
            creds_store.save(
                cfg.matrix.creds_path,
                creds_store.MatrixCreds(
                    homeserver=homeserver,
                    user_id=user_id,
                    access_token=access_token,
                    device_id=device_id,
                    control_room=control_room,
                    # Password login owns its device; an adopted token does
                    # not, and must never be logged out on our initiative.
                    minted=(method != "token"),
                ),
            )
            _ok(f"credentials written to {cfg.matrix.creds_path}")
        finally:
            # close() would also close our shared session; the `async with`
            # above owns it, so only drop nio's reference here.
            client.client_session = None

    _section("done")
    _say("  no restart needed - the running bridge watches this file and")
    _say("  switches accounts within a few seconds. Confirm with:")
    _say("      docker compose logs --tail 20")
    _say()
    if method == "token":
        _say("  reminder: this token is tied to an existing session. Do not log")
        _say("            that session out, or the bridge stops syncing.")
    else:
        _say("  note: this token belongs to the bridge's own device, so logging")
        _say("        out of Element sessions will no longer break it.")
    _say(RULE)
    return 0


def load_config_lenient(path: str) -> Config:
    """Load config for *defaults*, tolerating the very gaps we are here to fix.

    A dead token or a missing control room must not stop the tool whose whole
    job is to replace them.
    """
    try:
        return load_config(path)
    except ValueError:
        pass
    except FileNotFoundError:
        raise SystemExit(f"  config not found: {path}")

    # Re-read with validation bypassed by supplying a throwaway password.
    os.environ.setdefault("MATRIX_PASSWORD", "-")
    os.environ.setdefault("MATRIX_CONTROL_ROOM", "!unset:invalid")
    try:
        cfg = load_config(path)
    except ValueError as exc:
        # Anything still failing is a genuine config error (a bad proxy URL,
        # missing telegram keys) that this tool cannot paper over.
        raise SystemExit(f"  config error: {exc}") from None
    if cfg.matrix.password == "-":
        cfg.matrix.password = ""
    if cfg.matrix.control_room == "!unset:invalid":
        cfg.matrix.control_room = ""
    return cfg


def _choose_method(args: argparse.Namespace) -> str:
    """Password login or an existing access token?

    Password is the better default because it mints a token bound to a device
    the bridge owns. A pasted token is still needed for SSO-only accounts,
    where there is no password to log in with.
    """
    if args.token or args.token_stdin:
        return "token"
    if args.password_stdin:
        return "password"

    _say()
    _say("  how do you want to authenticate?")
    _say("    1) password  - mints a NEW token owned by the bridge (recommended)")
    _say("    2) token     - paste an existing access token (SSO accounts)")
    choice = _ask("choice", "1")
    return "token" if choice.strip() in ("2", "token", "t") else "password"


def _consume_stdin_secret(args: argparse.Namespace) -> None:
    """Take the piped secret before anything else can read stdin.

    Otherwise the first prompt swallows it: `input()` and the secret share one
    stdin, so a piped token would be answered into the homeserver question.
    Mirrors `docker login --password-stdin`, where stdin is *only* the secret.
    """
    args.secret = ""
    if not (args.password_stdin or args.token_stdin):
        return
    flag = "--token-stdin" if args.token_stdin else "--password-stdin"
    value = sys.stdin.readline().rstrip("\n").rstrip("\r")
    if not value:
        raise SystemExit(f"  {flag} given but stdin was empty")
    args.secret = value


def _read_secret(from_stdin: bool, label: str, flag: str, stashed: str = "") -> str:
    """Read a secret without echoing it, or use the one already piped in.

    Never taken as a command-line argument: argv is visible to other processes
    and lands in shell history.
    """
    if from_stdin:
        if not stashed:
            raise SystemExit(f"  {flag} given but stdin was empty")
        return stashed
    try:
        value = getpass.getpass(f"  {label} (not echoed): ")
    except EOFError:
        raise SystemExit(f"  no tty for the {label} - use {flag}")
    if not value:
        raise SystemExit(f"  {label} is required")
    return value


async def _auth_with_password(
    client: AsyncClient, args: argparse.Namespace, cfg: Config
) -> Optional[tuple[str, str, str]]:
    device_name = args.device_name or cfg.matrix.device_id or "MATRIX_TG_BRIDGE"
    password = _read_secret(
        args.password_stdin, "password", "--password-stdin", args.secret
    )

    resp = await client.login(password, device_name=device_name)
    if not isinstance(resp, LoginResponse):
        _fail(f"login rejected: {resp}")
        return None
    _ok(f"new device: {resp.device_id} ({device_name!r})")
    return resp.user_id, resp.access_token, resp.device_id


async def _auth_with_token(
    client: AsyncClient, args: argparse.Namespace
) -> Optional[tuple[str, str, str]]:
    """Adopt an existing access token, verifying it before we store it.

    The user id and device id come from /whoami rather than from what was
    typed: a token that disagrees with the account it claims to be would fail
    much later, in sync, as an unexplained silence.
    """
    token = _read_secret(
        args.token_stdin, "access token", "--token-stdin", args.secret
    )
    client.access_token = token

    resp = await client.whoami()
    if not isinstance(resp, WhoamiResponse):
        _fail(f"token rejected: {resp}")
        _warn("M_UNKNOWN_TOKEN means the token is dead - Element tokens die")
        _warn("when you log that Element session out. Copy a fresh one.")
        return None

    device_id = getattr(resp, "device_id", "") or ""
    _ok(f"token valid, device {device_id or '(unknown)'}")
    _warn("this token belongs to an EXISTING session, usually Element.")
    _warn("Logging that session out will kill the bridge. The password")
    _warn("flow avoids this by minting a token the bridge itself owns.")
    return resp.user_id, token, device_id


async def _ensure_room(client: AsyncClient, room: str) -> Optional[str]:
    """Join the control room, resolving a #alias:server to its room id."""
    if room.startswith("#"):
        resolved = await client.room_resolve_alias(room)
        room_id = getattr(resolved, "room_id", None)
        if not room_id:
            return None
        room = room_id
    resp = await client.join(room)
    return room if isinstance(resp, JoinResponse) else None


def _handle_account_switch(cfg: Config, new_user: str, assume_yes: bool) -> None:
    """Destroy the previous account's Matrix caches when the account changes.

    Two reasons, one action. Correctness: the nio store holds a sync position
    and device keys belonging to the *previous* user, and reusing it resumes
    from a foreign position, silently dropping messages. Privacy: the caches
    also hold relayed message text and a map of the old account's rooms, which
    the incoming account has no business inheriting.

    Telegram is untouched: its accounts and sessions have nothing to do with
    which Matrix account controls the bridge.
    """
    previous = creds_store.load(cfg.matrix.creds_path)
    if previous is None or previous.user_id == new_user:
        return

    _warn(f"account change: {previous.user_id} -> {new_user}")
    _warn("this discards the old account's cached message text, room map,")
    _warn("queued sends and sync store (telegram accounts are kept).")
    if not assume_yes and not _confirm("purge the previous account's data?", True):
        _warn("keeping it - expect sync oddities and stale rooms until cleared")
        return
    data_dir = os.path.dirname(cfg.options.state_path) or "."
    removed = purge_matrix_data(data_dir, cfg.matrix.store_path)
    for item in removed:
        _ok(f"purged {item}")
    if not removed:
        _ok("nothing to purge")


async def _revoke_superseded(
    cfg: Config, session, proxy, new_token: str, assume_yes: bool
) -> None:
    """Log the previous bridge-owned device out at the homeserver.

    A token this tool minted stays valid until somebody revokes it, and
    replacing the creds file does not. Without this, every switch leaves
    another live token able to read the control room. Only *minted* tokens
    qualify: an adopted one belongs to somebody's Element session, and logging
    that out would kill a session still in use.
    """
    previous = creds_store.load(cfg.matrix.creds_path)
    if previous is None or previous.access_token == new_token:
        return  # nothing is being superseded
    if not previous.minted:
        _warn("the previous token came from an existing session (not minted")
        _warn("here), so it is left alone. Revoke it in Element if you want")
        _warn("it gone - logging it out here would kill that session.")
        return

    device = previous.device_id or "?"
    _warn(f"the previous token ({previous.user_id}, device {device}) was")
    _warn("minted by this tool and is still valid at the homeserver.")
    if not assume_yes and not _confirm("log that device out now?", True):
        _warn("left active - revoke it yourself in Element > Sessions")
        return

    client = AsyncClient(
        previous.homeserver,
        previous.user_id,
        config=AsyncClientConfig(store_sync_tokens=False),
        proxy=http_proxy_url(proxy),
    )
    client.client_session = session
    client.user_id = previous.user_id
    client.access_token = previous.access_token
    try:
        resp = await client.logout()
        if isinstance(resp, LogoutResponse):
            _ok(f"previous device {device} logged out")
        else:
            _warn(f"could not revoke the previous token: {resp}")
            _warn("revoke it manually in Element > Settings > Sessions")
    except Exception as exc:  # noqa: BLE001 - never fail a completed login
        _warn(f"could not revoke the previous token ({type(exc).__name__})")
        _warn("revoke it manually in Element > Settings > Sessions")
    finally:
        # The `async with` in _run owns the shared session; only drop the ref.
        client.client_session = None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive Matrix account login for the bridge"
    )
    parser.add_argument("--config", default=os.getenv("BRIDGE_CONFIG", "config.yaml"))
    parser.add_argument("--homeserver", default="", help="skip the prompt")
    parser.add_argument("--user", default="", help="skip the prompt (@name:server)")
    parser.add_argument("--room", default="", help="skip the prompt (control room)")
    parser.add_argument("--device-name", default="", help="device label at the homeserver")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of prompting",
    )
    parser.add_argument(
        "--token",
        action="store_true",
        help="use an existing access token instead of a password (SSO accounts)",
    )
    parser.add_argument(
        "--token-stdin",
        action="store_true",
        help="read that access token from stdin (implies --token)",
    )
    parser.add_argument(
        "--no-egress-check",
        action="store_true",
        help="skip the outbound-IP lookup (avoids one third-party request)",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true", help="assume yes for confirmations"
    )
    args = parser.parse_args()

    try:
        sys.exit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        sys.exit("\n  aborted")


if __name__ == "__main__":
    main()
