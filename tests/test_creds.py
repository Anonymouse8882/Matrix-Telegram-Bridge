import json
import textwrap

import pytest

from bridge import creds as creds_store
from bridge.config import load_config

BASE = """
matrix:
  homeserver: https://matrix.org
  user_id: "@old:matrix.org"
  control_room: "!old:matrix.org"
  access_token: old-token
  password: old-pass
telegram:
  api_id: 111
  api_hash: deadbeef
options:
  state_path: {state}
"""


def _write(tmp_path, state_path):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(BASE.format(state=state_path)), encoding="utf-8")
    return str(p)


@pytest.fixture
def cfg_path(tmp_path):
    return _write(tmp_path, str(tmp_path / "state.json"))


def _creds(**over):
    base = dict(
        homeserver="https://new.example.org",
        user_id="@new:new.example.org",
        access_token="new-token",
        device_id="DEV123",
        control_room="!new:new.example.org",
    )
    base.update(over)
    return creds_store.MatrixCreds(**base)


def test_round_trip(tmp_path):
    path = str(tmp_path / "matrix_creds.json")
    creds_store.save(path, _creds())
    assert creds_store.load(path) == _creds()


def test_save_is_atomic_leaves_no_temp_file(tmp_path):
    path = str(tmp_path / "matrix_creds.json")
    creds_store.save(path, _creds())
    assert not (tmp_path / "matrix_creds.json.tmp").exists()


def test_missing_file_is_none(tmp_path):
    assert creds_store.load(str(tmp_path / "nope.json")) is None


@pytest.mark.parametrize(
    "content",
    ['{"broken": ', "[]", '{"user_id": "@a:b"}', '{"homeserver":"h","user_id":"","access_token":"t"}'],
)
def test_unusable_file_degrades_to_none(tmp_path, content):
    """A corrupt creds file must not take the bridge down - env/YAML still work."""
    path = tmp_path / "matrix_creds.json"
    path.write_text(content, encoding="utf-8")
    assert creds_store.load(str(path)) is None


def test_creds_file_overrides_yaml_and_env(cfg_path, tmp_path, monkeypatch):
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "from-env")
    creds_store.save(str(tmp_path / "matrix_creds.json"), _creds())

    cfg = load_config(cfg_path)

    assert cfg.matrix.access_token == "new-token"
    assert cfg.matrix.user_id == "@new:new.example.org"
    assert cfg.matrix.homeserver == "https://new.example.org"
    assert cfg.matrix.control_room == "!new:new.example.org"
    assert cfg.matrix.device_id == "DEV123"


def test_stored_creds_clear_stale_password(cfg_path, tmp_path):
    """A leftover password must not silently log the OLD account back in."""
    creds_store.save(str(tmp_path / "matrix_creds.json"), _creds())
    assert load_config(cfg_path).matrix.password == ""


def test_creds_path_defaults_next_to_state(cfg_path, tmp_path):
    cfg = load_config(cfg_path)
    assert cfg.matrix.creds_path == str(tmp_path / "matrix_creds.json")


def test_without_creds_file_yaml_still_wins(cfg_path):
    cfg = load_config(cfg_path)
    assert cfg.matrix.user_id == "@old:matrix.org"
    assert cfg.matrix.access_token == "old-token"


def test_bad_proxy_url_is_a_config_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(BASE.format(state=str(tmp_path / "state.json")))
        + '\nproxy:\n  url: "ftp://nope:1"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(str(path))


def test_proxy_url_from_env(cfg_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_PROXY", "socks5h://127.0.0.1:1080")
    assert load_config(cfg_path).proxy.url == "socks5h://127.0.0.1:1080"


def test_saved_file_is_valid_json(tmp_path):
    path = tmp_path / "matrix_creds.json"
    creds_store.save(str(path), _creds())
    assert json.loads(path.read_text(encoding="utf-8"))["device_id"] == "DEV123"
