"""What must and must not survive a Matrix account change."""

import json
import os

from bridge.purge import purge_matrix_data


def _write(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _seed(tmp_path):
    d = str(tmp_path)
    _write(os.path.join(d, "msglinks.json"), [
        {"chat_id": 111, "msg_id": 1, "kind": "user", "room_id": "!r:hs",
         "event_id": "$e", "ts": 1.0, "text": "a private sentence",
         "head": "", "media": False, "orig": ""},
    ])
    _write(os.path.join(d, "rooms.json"),
           {"rooms": {"111": "!r:hs"}, "names": {"111": "Alice"}})
    _write(os.path.join(d, "outbox.json"), [{"chat_id": 111, "text": "queued"}])
    _write(os.path.join(d, "expire.json"), [
        {"chat": 111, "msg": 9, "at": 99.0, "mx_room": "!r:hs", "mx_event": "$e"},
    ])
    _write(os.path.join(d, "state.json"), {
        "active": {"!r:hs": "111"},
        "muted": ["222"], "watched": ["-100333"],
        "self_destruct": {"user": 60, "group": 0, "channel": 0},
        "delay_fixed": 5, "delay_random": 0, "command_prefix": "!x",
    })
    store = os.path.join(d, "store")
    os.makedirs(store)
    with open(os.path.join(store, "sync.db"), "w", encoding="utf-8") as fh:
        fh.write("sync token")
    return d, store


def test_relayed_content_and_room_map_are_destroyed(tmp_path):
    d, store = _seed(tmp_path)
    purge_matrix_data(d, store)
    for gone in ("msglinks.json", "rooms.json", "outbox.json"):
        assert not os.path.exists(os.path.join(d, gone)), gone


def test_sync_store_is_emptied_but_kept(tmp_path):
    d, store = _seed(tmp_path)
    purge_matrix_data(d, store)
    assert os.path.isdir(store)      # nio expects the directory to exist
    assert os.listdir(store) == []


def test_telegram_settings_survive(tmp_path):
    """The Telegram account did not change, so its settings must not either."""
    d, store = _seed(tmp_path)
    purge_matrix_data(d, store)
    with open(os.path.join(d, "state.json"), encoding="utf-8") as fh:
        state = json.load(fh)
    assert state["muted"] == ["222"]
    assert state["watched"] == ["-100333"]
    assert state["self_destruct"]["user"] == 60
    assert state["delay_fixed"] == 5
    assert state["command_prefix"] == "!x"


def test_active_target_map_is_dropped(tmp_path):
    """Its keys are rooms of the account we just left."""
    d, store = _seed(tmp_path)
    purge_matrix_data(d, store)
    with open(os.path.join(d, "state.json"), encoding="utf-8") as fh:
        assert json.load(fh)["active"] == {}


def test_pending_self_destructs_keep_their_telegram_side(tmp_path):
    """Cancelling them would silently un-delete messages the user expects gone."""
    d, store = _seed(tmp_path)
    purge_matrix_data(d, store)
    with open(os.path.join(d, "expire.json"), encoding="utf-8") as fh:
        pending = json.load(fh)
    assert pending[0]["chat"] == 111 and pending[0]["msg"] == 9
    assert pending[0]["mx_room"] is None and pending[0]["mx_event"] is None


def test_crash_leftover_temp_files_are_swept(tmp_path):
    """`.tmp` holds a FULL copy — message text, or an access token."""
    d, store = _seed(tmp_path)
    for name in ("msglinks.json.tmp", "rooms.json.tmp", "matrix_creds.json.tmp"):
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write('{"leftover": "secret"}')

    purge_matrix_data(d, store)

    for name in ("msglinks.json.tmp", "rooms.json.tmp", "matrix_creds.json.tmp"):
        assert not os.path.exists(os.path.join(d, name)), name


def test_credentials_file_itself_is_left_for_the_switch_to_replace(tmp_path):
    d, store = _seed(tmp_path)
    creds = os.path.join(d, "matrix_creds.json")
    _write(creds, {"user_id": "@a:b"})
    purge_matrix_data(d, store)
    assert os.path.exists(creds)  # commit overwrites it; deleting would strand


def test_telegram_session_is_never_touched(tmp_path):
    """The Telegram account is not what changed."""
    d, store = _seed(tmp_path)
    session = os.path.join(d, "telegram.session")
    with open(session, "w", encoding="utf-8") as fh:
        fh.write("telethon")
    purge_matrix_data(d, store)
    assert os.path.exists(session)


def test_report_lists_what_went(tmp_path):
    d, store = _seed(tmp_path)
    removed = purge_matrix_data(d, store)
    assert "msglinks.json" in removed and "matrix store" in removed


def test_purging_a_clean_dir_is_a_no_op(tmp_path):
    assert purge_matrix_data(str(tmp_path), str(tmp_path / "nostore")) == []


# -- the caches actually live under accounts/tg-<id>/ ------------------------


def _seed_account(tmp_path, tg_id="tg-1234567890"):
    """The layout AccountRuntime really writes (runtime.py:87-118)."""
    d = str(tmp_path)
    acc = os.path.join(d, "accounts", tg_id)
    os.makedirs(acc)
    _write(os.path.join(acc, "msglinks.json"), [
        {"chat_id": 111, "msg_id": 1, "kind": "user", "room_id": "!r:hs",
         "event_id": "$e", "ts": 1.0, "text": "a private sentence",
         "head": "", "media": False, "orig": ""},
    ])
    _write(os.path.join(acc, "rooms.json"),
           {"rooms": {"111": "!oldacct-room:hs"}, "names": {"111": "Alice"}})
    _write(os.path.join(acc, "outbox.json"), [{"chat_id": 111, "text": "queued"}])
    _write(os.path.join(acc, "expire.json"), [
        {"chat": 111, "msg": 9, "at": 99.0, "mx_room": "!r:hs", "mx_event": "$e"},
    ])
    _write(os.path.join(acc, "state.json"), {
        "active": {"!r:hs": "111"}, "muted": ["222"], "watched": ["-100333"],
        "self_destruct": {"user": 60}, "delay_fixed": 5, "delay_random": 0,
        "command_prefix": "!x",
    })
    return d, acc


def test_per_account_caches_are_destroyed(tmp_path):
    d, acc = _seed_account(tmp_path)
    purge_matrix_data(d, os.path.join(d, "nostore"))
    for gone in ("msglinks.json", "rooms.json", "outbox.json"):
        assert not os.path.exists(os.path.join(acc, gone)), gone


def test_per_account_telegram_settings_survive(tmp_path):
    """A Matrix account change says nothing about the Telegram account."""
    d, acc = _seed_account(tmp_path)
    purge_matrix_data(d, os.path.join(d, "nostore"))
    state = json.load(open(os.path.join(acc, "state.json"), encoding="utf-8"))
    assert state["muted"] == ["222"] and state["watched"] == ["-100333"]
    assert state["delay_fixed"] == 5 and state["command_prefix"] == "!x"
    assert state["active"] == {}          # room pointers are the old account's


def test_per_account_self_destructs_keep_their_telegram_half(tmp_path):
    d, acc = _seed_account(tmp_path)
    purge_matrix_data(d, os.path.join(d, "nostore"))
    pending = json.load(open(os.path.join(acc, "expire.json"), encoding="utf-8"))
    assert pending[0]["chat"] == 111 and pending[0]["msg"] == 9
    assert pending[0]["mx_room"] is None and pending[0]["mx_event"] is None


def test_every_account_is_swept_not_just_the_first(tmp_path):
    d, first = _seed_account(tmp_path, "tg-111")
    _, second = _seed_account(tmp_path, "tg-222")
    purge_matrix_data(d, os.path.join(d, "nostore"))
    assert not os.path.exists(os.path.join(first, "msglinks.json"))
    assert not os.path.exists(os.path.join(second, "msglinks.json"))


def test_a_pending_telegram_login_is_left_alone(tmp_path):
    """`.pending` holds a half-finished Telegram sign-in, not Matrix data."""
    d, _ = _seed_account(tmp_path)
    pending = os.path.join(d, "accounts", ".pending")
    os.makedirs(pending)
    session = os.path.join(pending, "telegram.session")
    with open(session, "w", encoding="utf-8") as fh:
        fh.write("half-finished login")
    purge_matrix_data(d, os.path.join(d, "nostore"))
    assert os.path.exists(session)


def test_removed_items_name_the_account_they_came_from(tmp_path):
    d, _ = _seed_account(tmp_path, "tg-1234567890")
    removed = purge_matrix_data(d, os.path.join(d, "nostore"))
    assert "tg-1234567890/msglinks.json" in removed
