"""Outbound proxy support, shared by both sides of the bridge.

Every packet the bridge (and the login CLI) sends to matrix.org or Telegram
should be able to leave through one proxy, so the remote end never sees the
host's real address.

Two rules shape this module:

  * **Fail closed.** If a proxy is configured but cannot be applied — missing
    dependency, unparseable URL — we raise instead of quietly connecting
    direct. A silent fallback to a direct connection is exactly the leak the
    proxy exists to prevent.
  * **Remote DNS by default.** `socks5h://` resolves hostnames at the proxy;
    plain `socks5://` resolves them locally, which leaks the destination to
    whoever runs your resolver. We therefore treat bare `socks5` as opt-out and
    warn about it.

Kept SDK-agnostic in shape: it parses once, then hands each SDK the form it
wants (an aiohttp connector for nio, a dict for Telethon).
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import unquote, urlparse

log = logging.getLogger(__name__)

# Spellings that mean "inherit whatever this machine is already configured to
# use", and the ones that mean "deliberately no proxy".
_SYSTEM_ALIASES = {"", "system", "auto", "inherit"}
_DIRECT_ALIASES = {"none", "direct", "off", "no"}

# Checked in order; ALL_PROXY first because it is the only one that commonly
# carries a socks:// URL, and it applies to every scheme.
_PROXY_ENV_VARS = (
    "ALL_PROXY",
    "all_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
)

# socks5h/socks4a are the "resolve at the proxy" spellings of socks5/socks4.
_SCHEMES = {
    "socks5": ("socks5", False),
    "socks5h": ("socks5", True),
    "socks4": ("socks4", False),
    "socks4a": ("socks4", True),
    "http": ("http", True),  # CONNECT sends the hostname, so the proxy resolves
    "https": ("http", True),
}


class ProxyError(RuntimeError):
    """Proxy was configured but cannot be honoured — never fall back to direct."""


@dataclass(frozen=True)
class Proxy:
    kind: str  # "socks5" | "socks4" | "http"
    host: str
    port: int
    rdns: bool
    username: str = ""
    password: str = ""

    @property
    def is_socks(self) -> bool:
        return self.kind.startswith("socks")

    def sanitised(self) -> str:
        """Loggable form — credentials stripped."""
        return f"{self.kind}{'h' if self.rdns and self.is_socks else ''}://{self.host}:{self.port}"


def resolve_proxy_url(configured: str) -> tuple[str, str]:
    """Turn a configured value into a concrete URL plus where it came from.

    Returns (url, source) where source is one of "config", "system", "none".
    An empty setting means *system* rather than *direct*: on a machine behind a
    corporate proxy, silently going direct would be the surprising, leakier
    default. Say "none" to mean none.
    """
    val = (configured or "").strip()
    low = val.lower()
    if low in _DIRECT_ALIASES:
        return "", "none"
    if low not in _SYSTEM_ALIASES:
        return val, "config"

    detected = _detect_system_proxy()
    if detected:
        return detected, "system"
    # Nothing found. Note this is NOT a failure: a full-tunnel VPN (WireGuard,
    # Mullvad et al) sets no system proxy at all - it captures traffic at the
    # network layer, so "direct" already means "through the tunnel".
    return "", "none"


def _detect_system_proxy() -> str:
    for name in _PROXY_ENV_VARS:
        val = (os.environ.get(name) or "").strip()
        if val:
            log.debug("system proxy from %s", name)
            return val
    return _windows_system_proxy()


def _windows_system_proxy() -> str:
    """Read the WinINET proxy that the Settings app and browsers use.

    Only meaningful for local/dev runs - in Docker the bridge is on Linux and
    this is a no-op.
    """
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        with key:
            if not winreg.QueryValueEx(key, "ProxyEnable")[0]:
                return ""
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "").strip()
    except (OSError, ImportError, IndexError, TypeError):
        return ""
    return _parse_wininet_server(server)


def _parse_wininet_server(server: str) -> str:
    """WinINET stores either "host:port" or "http=h:p;https=h:p;socks=h:p"."""
    if not server:
        return ""
    if "=" not in server:
        return f"http://{server}"

    parts = {}
    for chunk in server.split(";"):
        if "=" in chunk:
            scheme, _, addr = chunk.partition("=")
            parts[scheme.strip().lower()] = addr.strip()
    # Prefer SOCKS: it is the only one Telegram's MTProto can use.
    if parts.get("socks"):
        return f"socks5h://{parts['socks']}"
    for scheme in ("https", "http"):
        if parts.get(scheme):
            return f"http://{parts[scheme]}"
    return ""


def parse_proxy(url: str) -> Optional[Proxy]:
    """Parse a proxy URL. Returns None only when *no* proxy was configured."""
    url = (url or "").strip()
    if not url:
        return None

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in _SCHEMES:
        raise ProxyError(
            f"unsupported proxy scheme {scheme!r} in {url!r}; "
            f"use one of: {', '.join(sorted(_SCHEMES))}"
        )
    if not parsed.hostname or not parsed.port:
        raise ProxyError(f"proxy URL needs an explicit host and port: {url!r}")

    kind, rdns = _SCHEMES[scheme]
    if kind == "socks5" and not rdns:
        log.warning(
            "proxy %s://%s uses LOCAL dns resolution - your resolver still sees "
            "every destination. Prefer socks5h:// for remote DNS.",
            scheme,
            parsed.hostname,
        )
    return Proxy(
        kind=kind,
        host=parsed.hostname,
        port=int(parsed.port),
        rdns=rdns,
        # Credentials may legitimately contain '@' / ':' when percent-encoded.
        username=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
    )


def telethon_proxy(proxy: Optional[Proxy]) -> Optional[dict[str, Any]]:
    """Telethon accepts a python-socks style dict for `TelegramClient(proxy=)`."""
    if proxy is None:
        return None
    if not proxy.is_socks:
        # MTProto is not HTTP; an HTTP CONNECT proxy is unreliable here and
        # failing loudly beats a connection that silently bypasses the proxy.
        raise ProxyError(
            "Telegram (MTProto) needs a SOCKS proxy; "
            f"{proxy.kind!r} cannot carry it. Use socks5h://..."
        )
    _require("python_socks", "python-socks[asyncio]", "Telegram SOCKS proxy")
    cfg: dict[str, Any] = {
        "proxy_type": proxy.kind,
        "addr": proxy.host,
        "port": proxy.port,
        "rdns": proxy.rdns,
    }
    if proxy.username:
        cfg["username"] = proxy.username
        cfg["password"] = proxy.password
    return cfg


def aiohttp_session(proxy: Optional[Proxy]) -> Optional[Any]:
    """Build the aiohttp session nio should use, or None to let nio make its own.

    nio's `proxy=` argument goes straight to aiohttp, which only understands
    HTTP proxies. For SOCKS we hand nio a pre-built session whose *connector*
    speaks SOCKS, which covers sync, sends and media downloads alike.
    """
    if proxy is None:
        return None

    import aiohttp

    if not proxy.is_socks:
        return None  # caller uses nio's own `proxy=` for plain HTTP proxies

    _require("aiohttp_socks", "aiohttp-socks", "Matrix SOCKS proxy")
    from aiohttp_socks import ProxyConnector, ProxyType

    connector = ProxyConnector(
        proxy_type=ProxyType.SOCKS5 if proxy.kind == "socks5" else ProxyType.SOCKS4,
        host=proxy.host,
        port=proxy.port,
        rdns=proxy.rdns,
        username=proxy.username or None,
        password=proxy.password or None,
    )
    return aiohttp.ClientSession(connector=connector)


def http_proxy_url(proxy: Optional[Proxy]) -> Optional[str]:
    """The `proxy=` string for aiohttp/nio, for HTTP proxies only."""
    if proxy is None or proxy.is_socks:
        return None
    auth = ""
    if proxy.username:
        auth = f"{proxy.username}:{proxy.password}@"
    return f"http://{auth}{proxy.host}:{proxy.port}"


def _require(module: str, pip_name: str, what: str) -> None:
    """Fail closed when the transport library for a configured proxy is absent."""
    import importlib.util

    if importlib.util.find_spec(module) is None:
        raise ProxyError(
            f"{what} requires the {pip_name!r} package "
            f"(pip install '{pip_name}'). Refusing to connect directly."
        )
