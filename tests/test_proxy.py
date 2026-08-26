import pytest

from bridge.proxy import ProxyError, http_proxy_url, parse_proxy, telethon_proxy


def test_no_proxy_configured_is_none():
    assert parse_proxy("") is None
    assert parse_proxy("   ") is None


def test_socks5h_enables_remote_dns():
    p = parse_proxy("socks5h://127.0.0.1:1080")
    assert (p.kind, p.host, p.port, p.rdns) == ("socks5", "127.0.0.1", 1080, True)


def test_socks5_keeps_local_dns():
    """Bare socks5 must NOT silently become socks5h - the leak is the default."""
    assert parse_proxy("socks5://127.0.0.1:1080").rdns is False


def test_credentials_are_percent_decoded():
    p = parse_proxy("socks5h://user:p%40ss%3Aword@10.0.0.2:9050")
    assert p.username == "user"
    assert p.password == "p@ss:word"


def test_sanitised_form_hides_credentials():
    out = parse_proxy("socks5h://user:hunter2@10.0.0.2:9050").sanitised()
    assert "hunter2" not in out and "user" not in out
    assert out == "socks5h://10.0.0.2:9050"


@pytest.mark.parametrize(
    "url",
    ["ftp://host:1080", "socks5h://host", "socks5h://:1080", "not-a-url"],
)
def test_bad_proxy_urls_raise(url):
    """Fail closed: an unusable proxy is an error, never a direct connection."""
    with pytest.raises(ProxyError):
        parse_proxy(url)


def test_http_proxy_url_only_for_http():
    assert http_proxy_url(parse_proxy("socks5h://h:1")) is None
    assert http_proxy_url(parse_proxy("http://h:3128")) == "http://h:3128"
    assert http_proxy_url(None) is None


def test_http_proxy_url_includes_auth():
    assert http_proxy_url(parse_proxy("http://u:p@h:3128")) == "http://u:p@h:3128"


def test_telethon_rejects_http_proxy():
    """MTProto cannot ride an HTTP CONNECT proxy; better to say so loudly."""
    with pytest.raises(ProxyError):
        telethon_proxy(parse_proxy("http://h:3128"))


def test_telethon_proxy_dict():
    cfg = telethon_proxy(parse_proxy("socks5h://u:p@10.0.0.2:9050"))
    assert cfg == {
        "proxy_type": "socks5",
        "addr": "10.0.0.2",
        "port": 9050,
        "rdns": True,
        "username": "u",
        "password": "p",
    }


def test_telethon_proxy_none_passthrough():
    assert telethon_proxy(None) is None
