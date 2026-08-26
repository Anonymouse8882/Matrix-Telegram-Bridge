import pytest

from bridge.proxy import _parse_wininet_server, resolve_proxy_url

ENV_VARS = (
    "ALL_PROXY",
    "all_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Detection must not inherit the developer's own proxy settings."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Stop the Windows registry lookup from leaking into these tests.
    monkeypatch.setattr("bridge.proxy._windows_system_proxy", lambda: "")


@pytest.mark.parametrize("value", ["", "  ", "system", "SYSTEM", "auto", "inherit"])
def test_blank_and_aliases_mean_system(value, monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5h://10.0.0.1:1080")
    assert resolve_proxy_url(value) == ("socks5h://10.0.0.1:1080", "system")


@pytest.mark.parametrize("value", ["none", "direct", "off", "NONE"])
def test_explicit_opt_out_ignores_environment(value, monkeypatch):
    """Saying "none" must win over an ambient proxy - it is a deliberate choice."""
    monkeypatch.setenv("ALL_PROXY", "socks5h://10.0.0.1:1080")
    assert resolve_proxy_url(value) == ("", "none")


def test_explicit_url_wins_over_environment(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5h://10.0.0.1:1080")
    assert resolve_proxy_url("socks5h://127.0.0.1:9050") == (
        "socks5h://127.0.0.1:9050",
        "config",
    )


def test_no_system_proxy_is_direct_not_an_error():
    """A full-tunnel VPN sets no proxy; that is normal, not a failure."""
    assert resolve_proxy_url("system") == ("", "none")


def test_all_proxy_preferred_over_http_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.9:3128")
    monkeypatch.setenv("ALL_PROXY", "socks5h://10.0.0.1:1080")
    assert resolve_proxy_url("system")[0] == "socks5h://10.0.0.1:1080"


def test_falls_through_to_http_proxy(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://10.0.0.9:3128")
    assert resolve_proxy_url("system") == ("http://10.0.0.9:3128", "system")


def test_whitespace_only_env_var_is_ignored(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "   ")
    assert resolve_proxy_url("system") == ("", "none")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10.0.0.1:8080", "http://10.0.0.1:8080"),
        ("http=10.0.0.1:8080;https=10.0.0.2:8443", "http://10.0.0.2:8443"),
        # SOCKS wins: it is the only form Telegram's MTProto can use.
        ("http=10.0.0.1:8080;socks=10.0.0.3:1080", "socks5h://10.0.0.3:1080"),
        ("", ""),
        ("ftp=10.0.0.1:21", ""),
    ],
)
def test_wininet_server_shapes(raw, expected):
    assert _parse_wininet_server(raw) == expected
