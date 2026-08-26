"""Configuration loading.

Reads a YAML file into typed dataclasses, then lets environment variables
override the secret fields so tokens never have to live in the file (handy for
Docker / CI). Kept separate from every other module so wiring code depends on
plain dataclasses, not on YAML.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

from . import creds as creds_store


def _env_or(name: str, fallback: str) -> str:
    """Env override, but an *empty* env var falls back to the file value.

    docker-compose's `${VAR:-}` sets absent vars to "" in the container; a
    plain os.getenv would then wipe the config.yaml value. Treat "" as unset.
    """
    val = os.environ.get(name)
    return val if val else fallback


@dataclass
class MatrixConfig:
    homeserver: str
    user_id: str
    control_room: str = ""  # room used to control TG and view incoming messages
    access_token: str = ""
    password: str = ""
    device_id: str = "MATRIX_TG_BRIDGE"
    store_path: str = "./store"
    # Written by bridge.mxlogin; takes precedence over env/YAML (see load_config).
    creds_path: str = ""
    # Space to file per-chat rooms under. Empty = per-chat rooms disabled
    # (everything relays into the control room, the pre-space behaviour).
    space: str = ""


@dataclass
class TelegramConfig:
    """Real Telegram account via MTProto (Telethon)."""

    api_id: int = 0
    api_hash: str = ""
    session: str = "telegram.session"
    phone: str = ""
    # Telethon otherwise reports the real platform/uname to Telegram, which is
    # a host fingerprint. Pin it to something generic and stable instead.
    device_model: str = "Desktop"
    system_version: str = "Windows 10"
    app_version: str = "5.3.1"
    lang_code: str = "en"


@dataclass
class ProxyConfig:
    """One outbound proxy for both Matrix and Telegram.

    `url` is the *resolved* address; blank means direct. `source` records how
    we got there ("config" / "system" / "none") so startup can say so out loud.
    """

    url: str = ""  # e.g. socks5h://127.0.0.1:1080
    source: str = "none"
    configured: str = ""  # the raw setting, e.g. "system"


@dataclass
class Options:
    command_prefix: str = "!tg"
    default_target: str = ""  # optional initial active target
    state_path: str = "./state.json"  # where active-target / mutes persist
    timezone: str = "UTC"  # IANA name, used for `!tg at` scheduled sends


@dataclass
class Config:
    matrix: MatrixConfig
    telegram: TelegramConfig
    options: Options = field(default_factory=Options)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    mx = data.get("matrix", {})
    tg = data.get("telegram", {})
    opt = data.get("options", {})
    px = data.get("proxy", {})

    matrix = MatrixConfig(
        homeserver=_env_or("MATRIX_HOMESERVER", mx.get("homeserver", "")),
        user_id=_env_or("MATRIX_USER_ID", mx.get("user_id", "")),
        control_room=_env_or("MATRIX_CONTROL_ROOM", mx.get("control_room", "")),
        access_token=_env_or("MATRIX_ACCESS_TOKEN", mx.get("access_token", "")),
        password=_env_or("MATRIX_PASSWORD", mx.get("password", "")),
        device_id=mx.get("device_id", "MATRIX_TG_BRIDGE"),
        store_path=mx.get("store_path", "./store"),
        creds_path=_env_or("MATRIX_CREDS_PATH", mx.get("creds_path", "")),
        space=_env_or("MATRIX_SPACE", mx.get("space", "")),
    )
    api_id_raw = _env_or("TELEGRAM_API_ID", str(tg.get("api_id", "") or ""))
    telegram = TelegramConfig(
        api_id=int(api_id_raw) if api_id_raw else 0,
        api_hash=_env_or("TELEGRAM_API_HASH", tg.get("api_hash", "")),
        session=tg.get("session", "telegram.session"),
        phone=_env_or("TELEGRAM_PHONE", tg.get("phone", "")),
        device_model=str(tg.get("device_model", "Desktop")),
        system_version=str(tg.get("system_version", "Windows 10")),
        app_version=str(tg.get("app_version", "5.3.1")),
        lang_code=str(tg.get("lang_code", "en")),
    )
    from .proxy import resolve_proxy_url

    # Default to the system proxy: on a machine that has one, going direct
    # instead would be the leakier surprise. "none" opts out explicitly.
    raw_proxy = _env_or("BRIDGE_PROXY", str(px.get("url", "system") or "system"))
    proxy_url, proxy_source = resolve_proxy_url(raw_proxy)
    proxy = ProxyConfig(url=proxy_url, source=proxy_source, configured=raw_proxy)
    options = Options(
        command_prefix=str(opt.get("command_prefix", "!tg")),
        default_target=str(opt.get("default_target", "")),
        state_path=str(opt.get("state_path", "./state.json")),
        timezone=str(opt.get("timezone", "UTC")),
    )

    cfg = Config(matrix=matrix, telegram=telegram, options=options, proxy=proxy)
    if not cfg.matrix.creds_path:
        cfg.matrix.creds_path = default_creds_path(cfg)
    _apply_stored_creds(cfg)
    _validate(cfg)
    return cfg


def default_creds_path(cfg: Config) -> str:
    """Sit next to state.json, i.e. on the mounted /data volume in Docker."""
    data_dir = os.path.dirname(cfg.options.state_path) or "."
    return os.path.join(data_dir, creds_store.DEFAULT_FILENAME)


def _apply_stored_creds(cfg: Config) -> None:
    """Overlay credentials minted by `bridge.mxlogin`.

    These win over env/YAML: they are the most recent deliberate act by the
    operator, and they are what makes swapping the Matrix account a restart
    rather than a container recreate.
    """
    stored = creds_store.load(cfg.matrix.creds_path)
    if stored is None:
        return

    cfg.matrix.homeserver = stored.homeserver
    cfg.matrix.user_id = stored.user_id
    cfg.matrix.access_token = stored.access_token
    # A stale password must not resurrect the previous account if this token
    # ever fails; the creds file is the single source of truth once present.
    cfg.matrix.password = ""
    if stored.device_id:
        cfg.matrix.device_id = stored.device_id
    if stored.control_room:
        cfg.matrix.control_room = stored.control_room


def _validate(cfg: Config) -> None:
    # Parse the proxy now so a typo fails at startup, not on the first request
    # (a proxy that silently does not apply is a privacy bug, not a nuisance).
    from .proxy import ProxyError, parse_proxy

    try:
        parse_proxy(cfg.proxy.url)
    except ProxyError as exc:
        raise ValueError(str(exc)) from exc

    if not cfg.matrix.homeserver or not cfg.matrix.user_id:
        raise ValueError("matrix.homeserver and matrix.user_id are required")
    if not cfg.matrix.access_token and not cfg.matrix.password:
        raise ValueError("provide either matrix.access_token or matrix.password")
    if not cfg.matrix.control_room:
        raise ValueError("matrix.control_room is required (the Element room id)")
    if not cfg.telegram.api_id or not cfg.telegram.api_hash:
        raise ValueError(
            "telegram.api_id and telegram.api_hash are required "
            "(get them from https://my.telegram.org ▸ API development tools)"
        )
