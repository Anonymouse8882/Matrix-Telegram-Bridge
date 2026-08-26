import textwrap

import pytest

from bridge.config import load_config


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(p)


BASE = """
matrix:
  homeserver: https://matrix.org
  user_id: "@bot:matrix.org"
  control_room: "!room:matrix.org"
  access_token: tok
telegram:
  api_id: 111
  api_hash: deadbeef
  session: /data/telegram.session
options:
  command_prefix: "!tg"
  default_target: "@somechan"
"""


def test_load_ok(tmp_path):
    cfg = load_config(_write(tmp_path, BASE))
    assert cfg.matrix.user_id == "@bot:matrix.org"
    assert cfg.matrix.control_room == "!room:matrix.org"
    assert cfg.telegram.api_id == 111
    assert cfg.telegram.api_hash == "deadbeef"
    assert cfg.options.command_prefix == "!tg"
    assert cfg.options.default_target == "@somechan"


def test_env_overrides_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_HASH", "from-env")
    monkeypatch.setenv("TELEGRAM_API_ID", "999")
    monkeypatch.setenv("MATRIX_CONTROL_ROOM", "!other:matrix.org")
    cfg = load_config(_write(tmp_path, BASE))
    assert cfg.telegram.api_hash == "from-env"
    assert cfg.telegram.api_id == 999
    assert cfg.matrix.control_room == "!other:matrix.org"


def test_empty_env_does_not_override_file(tmp_path, monkeypatch):
    # docker-compose's `${VAR:-}` sets empty env vars; they must NOT wipe the
    # config.yaml values (regression: matrix.homeserver became empty in Docker).
    monkeypatch.setenv("MATRIX_HOMESERVER", "")
    monkeypatch.setenv("MATRIX_USER_ID", "")
    monkeypatch.setenv("TELEGRAM_API_HASH", "")
    cfg = load_config(_write(tmp_path, BASE))
    assert cfg.matrix.homeserver == "https://matrix.org"
    assert cfg.matrix.user_id == "@bot:matrix.org"
    assert cfg.telegram.api_hash == "deadbeef"


def test_missing_control_room_rejected(tmp_path):
    bad = BASE.replace('control_room: "!room:matrix.org"', "")
    with pytest.raises(ValueError, match="control_room"):
        load_config(_write(tmp_path, bad))


def test_missing_api_creds_rejected(tmp_path):
    bad = BASE.replace("api_id: 111", "api_id: 0")
    with pytest.raises(ValueError, match="api_id"):
        load_config(_write(tmp_path, bad))


def test_missing_matrix_credentials_rejected(tmp_path):
    bad = BASE.replace("access_token: tok", "")
    with pytest.raises(ValueError):
        load_config(_write(tmp_path, bad))
