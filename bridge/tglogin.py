"""Interactive Telegram account login — add an account from a real terminal.

The safe counterpart of `!tg login`: the phone code and 2FA password are typed
here rather than into a Matrix room, so they never reach the homeserver at all.
Either way the account lands in the same list, and `!tg accounts` shows both.

    python -m bridge.tglogin --config config.yaml            # add an account
    python -m bridge.tglogin --config config.yaml --list     # show them
    python -m bridge.tglogin --config config.yaml --space '!id:server'

Each account gets its own session under `accounts/tg-<id>/`, so several can be
logged in at once. A `--space` binds it to a Matrix Space immediately; without
one the account relays into the control room until `!tg bind` says otherwise.

Sessions and the account list live on the data volume, so a login here is
picked up by the running bridge on its next restart.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil

from telethon import TelegramClient

from . import accounts as vault
from .config import Config, load_config
from .proxy import parse_proxy, telethon_proxy


def _data_dir(cfg: Config) -> str:
    return os.path.dirname(cfg.options.state_path) or "."


def _vault_path(cfg: Config) -> str:
    return os.path.join(_data_dir(cfg), vault.DEFAULT_FILENAME)


def _client(cfg: Config, session: str) -> TelegramClient:
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


def _list(cfg: Config) -> None:
    accounts = vault.load(_vault_path(cfg))
    if not accounts:
        print("no telegram accounts yet - run without --list to add one")
        return
    print(f"{len(accounts)} telegram account(s):")
    for i, a in enumerate(accounts, 1):
        space = a.space_id or "(no space bound)"
        print(f"  {i}. {a.label}  id={a.tg_id}  {a.phone}  space={space}")


async def _login(cfg: Config, phone: str, space: str) -> None:
    # The Telegram id is only known after sign-in, so the session starts in a
    # scratch directory and moves to `tg-<id>/` once the account names itself.
    scratch = os.path.join(_data_dir(cfg), "accounts", ".pending-cli")
    shutil.rmtree(scratch, ignore_errors=True)
    client = _client(cfg, os.path.join(scratch, vault.SESSION_FILENAME))

    # start() drives the interactive phone/code/2FA flow as needed.
    await client.start(
        phone=(lambda: phone or cfg.telegram.phone or input("Phone (+countrycode): "))
    )
    me = await client.get_me()
    tg_id = int(me.id)
    name = " ".join(
        p for p in (getattr(me, "first_name", ""), getattr(me, "last_name", "")) if p
    ).strip()
    await client.disconnect()

    # Only the session file moves. Signing in again as an account that already
    # exists is a repair, not a reset: its room map, message links and settings
    # are keyed by Telegram chat id and stay valid across a new session.
    existing = os.path.isdir(vault.account_dir(_data_dir(cfg), tg_id))
    vault.install_session(scratch, _data_dir(cfg), tg_id)

    account = vault.TelegramAccount(
        tg_id=tg_id, name=name, username=getattr(me, "username", "") or "",
        phone=phone or cfg.telegram.phone, space_id=space,
    )
    vault.upsert(_vault_path(cfg), account)

    print(f"\n✅ Logged in as {account.label} (id {tg_id})")
    print(f"   session: {vault.session_path(_data_dir(cfg), tg_id)}")
    if existing:
        print("   kept this account's existing rooms, message links and settings")
    if space:
        print(f"   bound to space: {space}")
    else:
        print("   no space bound yet - run `!tg bind` in a room inside the")
        print("   Space you want this account's chats filed under.")
    print("   restart the bridge to bring it online: docker compose restart")


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram user-account login")
    parser.add_argument(
        "--config", default=os.getenv("BRIDGE_CONFIG", "config.yaml")
    )
    parser.add_argument("--phone", default="", help="skip the prompt (+countrycode)")
    parser.add_argument("--space", default="", help="Matrix Space id to bind")
    parser.add_argument(
        "--list", action="store_true", help="list saved accounts and exit"
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.list:
        _list(cfg)
        return
    asyncio.run(_login(cfg, args.phone, args.space))


if __name__ == "__main__":
    main()
