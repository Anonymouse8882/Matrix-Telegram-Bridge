"""Composition root: the one place adapters and core are wired together.

One Matrix account controls the bridge; several Telegram accounts run under it,
each in its own `AccountRuntime` with its own Space, rooms and caches.

Outgoing:  MatrixSource ─▶ Dispatcher ─▶ (account) TelegramUserSink
Incoming:  (account) TelegramUserSource ─▶ Relay ─▶ MatrixSink

Run with:  python -m bridge --config config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import signal
import sys

from .adapters.matrix_sink import MatrixSink
from .adapters.matrix_source import MatrixSource
from .adapters.matrix_spaces import MatrixSpaces
from .config import Config, load_config  # noqa: F401  (Config used in annotations)
from .core.dispatcher import Dispatcher
from .core.state import BridgeState
from .proxy import ProxyError, parse_proxy
from .purge import purge_matrix_data
from .runtime import AccountManager

log = logging.getLogger(__name__)


class App:
    """Holds the wired components and their shared lifecycles."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # Only the command prefix is global now: everything else about how the
        # bridge behaves belongs to a Telegram account and lives in its own
        # state file (see AccountRuntime).
        self.state = BridgeState(cfg.options.state_path)
        self._data_dir = os.path.dirname(cfg.options.state_path) or "."

        # --- Matrix side (one shared nio client, one account) ---
        self.matrix_source = MatrixSource(
            homeserver=cfg.matrix.homeserver,
            user_id=cfg.matrix.user_id,
            access_token=cfg.matrix.access_token,
            password=cfg.matrix.password,
            device_id=cfg.matrix.device_id,
            store_path=cfg.matrix.store_path,
            watched_rooms={cfg.matrix.control_room},
            proxy_url=cfg.proxy.url,
            # Account commands have to work inside a Space's rooms, which are
            # not bridge rooms — so commands are accepted anywhere.
            command_prefix=self._command_prefix,
        )
        self.matrix_sink = MatrixSink(self.matrix_source.client)
        self.spaces = MatrixSpaces(self.matrix_source.client)

        # --- Telegram side: one runtime per logged-in account ---
        self.accounts = AccountManager(
            cfg=cfg,
            data_dir=self._data_dir,
            matrix_client=self.matrix_source.client,
            matrix_sink=self.matrix_sink,
            matrix_fetcher=self.matrix_source,  # re-fetches Matrix media to send
            root_control_room=cfg.matrix.control_room,
            homeserver_name=_server_name(cfg.matrix.user_id),
            on_new_room=self.matrix_source.watch,
        )

        # --- core orchestration ---
        self.dispatcher = Dispatcher(
            accounts=self.accounts,
            matrix_replier=self.matrix_sink,
            state=self.state,
            control_room=cfg.matrix.control_room,
            command_prefix=cfg.options.command_prefix,
            default_target=cfg.options.default_target,
            timezone=cfg.options.timezone,
            on_new_room=self.matrix_source.watch,
            redactor=self.matrix_sink,  # deletes messages carrying a phone code
            spaces=self.spaces,
        )
        self.matrix_source.set_handler(self.dispatcher.on_matrix_message)
        self.matrix_source.set_redaction_handler(self.dispatcher.on_redaction)
        # A send that fails minutes after it was queued has no reply to attach
        # to, so the dispatcher reports it out of band.
        self.accounts.set_send_failure_handler(self.dispatcher.on_send_failed)

    def _command_prefix(self) -> str:
        return self.state.command_prefix() or self.cfg.options.command_prefix

    async def start(self) -> None:
        account_tasks = await self.accounts.start_all()
        if not account_tasks:
            log.warning(
                "no telegram account is logged in - the bridge will run, but "
                "only account commands work. Log in with `!tg login <phone>` "
                "in Matrix, or `python -m bridge.tglogin`."
            )
        # Per-chat rooms of every account have to be synced from Matrix too.
        for room in self.accounts.watched_rooms():
            self.matrix_source.watch(room)

        # The Matrix sync is the app's spine: when it ends, the app ends.
        # Account tasks run alongside, each observed by its runtime — one
        # crashed Telegram account must not take the whole bridge down.
        await self.matrix_source.start()

    async def close(self) -> None:
        await self.accounts.stop_all()
        await self.matrix_source.close()


def _log_egress(cfg: Config) -> None:
    """State the egress path at startup.

    Whether traffic is proxied is a privacy-relevant fact, so it belongs in the
    log where it can be checked after the fact - not inferred from config.
    """
    proxy = parse_proxy(cfg.proxy.url)
    if proxy is not None:
        log.info("egress: via %s (from %s)", proxy.sanitised(), cfg.proxy.source)
    elif cfg.proxy.configured.strip().lower() in ("none", "direct", "off", "no"):
        log.info("egress: DIRECT (proxy explicitly disabled)")
    else:
        log.info(
            "egress: DIRECT - no system proxy found (expected behind a "
            "full-tunnel VPN; a leak otherwise)"
        )


def _server_name(user_id: str) -> str:
    """The homeserver's DNS name from a user id (@name:server -> server)."""
    _, _, server = user_id.partition(":")
    return server or "matrix.org"


def _creds_fingerprint(path: str) -> str:
    """Content hash of the credentials file, or "" when absent.

    Content rather than mtime: `creds.save` writes a temp file and renames it,
    and a rename can leave mtime looking unchanged on some filesystems. A hash
    also means rewriting identical credentials does not trigger a reload.
    """
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


async def _watch_creds(path: str, baseline: str, changed: asyncio.Event) -> None:
    """Set `changed` when the credentials file differs from `baseline`.

    Polling beats inotify here: the file is written from a *different
    container* onto a bind mount, where filesystem events are unreliable
    (notably on Docker Desktop). One stat per 2s costs nothing.
    """
    while True:
        await asyncio.sleep(2)
        if _creds_fingerprint(path) != baseline:
            changed.set()
            return


def _purge_previous(cfg: Config) -> None:
    """Clear the previous Matrix account's caches after its app has stopped.

    Deliberately a second pass. `bridge.mxlogin` purges too, but it runs as a
    separate container against files this one holds in memory and rewrites on
    the next relayed message or new room — so between its purge and this
    teardown, some of them come back. Here nothing is writing any more.
    """
    data_dir = os.path.dirname(cfg.options.state_path) or "."
    removed = purge_matrix_data(data_dir, cfg.matrix.store_path)
    if removed:
        log.info("purged the previous account's caches: %s", ", ".join(removed))


async def _run(config_path: str) -> None:
    """Supervise the app, rebuilding it in place when credentials change.

    `bridge.mxlogin` writes new Matrix credentials to the shared /data volume;
    we notice, tear the wiring down and rebuild it against the new account.
    That is what makes switching the control account hot rather than a restart.
    """
    stop = asyncio.Event()

    def _request_stop(*_: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # Windows lacks add_signal_handler for SIGTERM
            signal.signal(sig, _request_stop)

    cfg = load_config(config_path)
    stopper = asyncio.create_task(stop.wait())
    failure: BaseException | None = None

    while not stop.is_set():
        _log_egress(cfg)
        app = App(cfg)
        creds_path = cfg.matrix.creds_path
        baseline = _creds_fingerprint(creds_path)

        reload_requested = asyncio.Event()
        watcher = asyncio.create_task(
            _watch_creds(creds_path, baseline, reload_requested)
        )
        reloader = asyncio.create_task(reload_requested.wait())
        runner = asyncio.create_task(app.start())

        await asyncio.wait(
            {runner, stopper, reloader}, return_when=asyncio.FIRST_COMPLETED
        )

        for task in (runner, watcher, reloader):
            task.cancel()
        results = await asyncio.gather(
            runner, watcher, reloader, return_exceptions=True
        )
        await app.close()

        # The app's own failure is the one result that must never be swallowed.
        # Without this, a rejected login or a dead token ends the process with
        # exit 0 and an empty log, and the container restart-loops in silence.
        outcome = results[0]
        if isinstance(outcome, BaseException) and not isinstance(
            outcome, asyncio.CancelledError
        ):
            log.error("bridge stopped with an error", exc_info=outcome)
            failure = outcome

        if stop.is_set() or failure is not None or not reload_requested.is_set():
            break  # shutdown, a crash, or the app exited - do not respawn

        previous_user = cfg.matrix.user_id
        # Validate the new config BEFORE committing to it: a broken creds file
        # must not turn an account switch into an outage.
        try:
            cfg = load_config(config_path)
        except (ValueError, ProxyError) as exc:
            log.error("credentials changed but config is invalid: %s", exc)
            log.error("keeping the previous account; fix it and save again")
            continue  # cfg is unchanged, so the loop rebuilds the old account
        log.info("credentials changed - reloading as %s", cfg.matrix.user_id)
        if cfg.matrix.user_id != previous_user:
            # Purge again, now that the previous account's app is fully stopped.
            # `bridge.mxlogin` already purged, but it runs in a separate
            # container while this one is still live and still writing these
            # caches back from memory — so its purge can be partly undone.
            _purge_previous(cfg)

    stopper.cancel()
    if failure is not None:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Matrix <-> Telegram bridge")
    parser.add_argument(
        "--config",
        default=os.getenv("BRIDGE_CONFIG", "config.yaml"),
        help="path to config.yaml",
    )
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # These libraries log every synced event / download at INFO — too noisy.
    for noisy in ("nio", "telethon"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        asyncio.run(_run(args.config))
    except FileNotFoundError:
        sys.exit(f"config not found: {args.config} "
                 f"(copy config.example.yaml and set --config / BRIDGE_CONFIG)")
    except ValueError as exc:
        sys.exit(f"invalid config: {exc}")
    except ProxyError as exc:
        # Fail closed: never fall back to a direct connection.
        sys.exit(f"proxy unusable, refusing to connect directly: {exc}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
