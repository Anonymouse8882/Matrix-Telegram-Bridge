"""The supervisor loop in __main__: what happens when the app dies or reloads."""

import asyncio
import json
import os

import pytest

import bridge.__main__ as entry


def _config(tmp_path, user="@a:hs") -> str:
    path = tmp_path / "config.yaml"
    path.write_text(f"""
matrix:
  homeserver: "https://hs.invalid"
  user_id: "{user}"
  control_room: "!c:hs"
  access_token: "tok"
  store_path: "{tmp_path}/store"
  creds_path: "{tmp_path}/creds.json"
telegram: {{api_id: 1, api_hash: "h"}}
options: {{state_path: "{tmp_path}/state.json"}}
proxy: {{url: "none"}}
""", encoding="utf-8")
    return str(path)


class _Boom:
    """An App whose start() fails the way a rejected login does."""

    def __init__(self, cfg):
        pass

    async def start(self):
        raise RuntimeError("matrix login failed: M_UNKNOWN_TOKEN")

    async def close(self):
        pass


async def test_a_fatal_startup_error_is_logged_and_exits_nonzero(
    tmp_path, monkeypatch, caplog
):
    """Swallowing it left the container restart-looping with an empty log."""
    monkeypatch.setattr(entry, "App", _Boom)
    with caplog.at_level("ERROR"):
        with pytest.raises(SystemExit) as exit_info:
            await entry._run(_config(tmp_path))
    assert exit_info.value.code == 1
    assert "M_UNKNOWN_TOKEN" in caplog.text


async def test_a_clean_exit_stays_quiet(tmp_path, monkeypatch, caplog):
    class _Done:
        def __init__(self, cfg): pass
        async def start(self): return None      # sync_forever returned
        async def close(self): pass

    monkeypatch.setattr(entry, "App", _Done)
    with caplog.at_level("ERROR"):
        await entry._run(_config(tmp_path))     # no SystemExit
    assert "error" not in caplog.text.lower()


# -- the reload purge --------------------------------------------------------


def test_purge_previous_clears_the_account_caches(tmp_path):
    """mxlogin purges from another container while this one still writes
    these files back; the reload repeats it once nothing is writing."""
    from bridge.config import load_config

    acc = tmp_path / "accounts" / "tg-1"
    acc.mkdir(parents=True)
    (acc / "msglinks.json").write_text('[{"chat_id": 1}]', encoding="utf-8")
    (acc / "rooms.json").write_text('{"rooms": {"1": "!old:hs"}}', encoding="utf-8")
    store = tmp_path / "store"
    store.mkdir()
    (store / "sync.db").write_text("old position", encoding="utf-8")

    entry._purge_previous(load_config(_config(tmp_path)))

    assert not (acc / "msglinks.json").exists()
    assert not (acc / "rooms.json").exists()
    assert not (store / "sync.db").exists()
