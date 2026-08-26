"""The Telegram account vault: many accounts, one current, one Space each."""

import json
import os

from bridge import accounts as vault


def _path(tmp_path):
    return str(tmp_path / "telegram_accounts.json")


def _a(tg_id=1001, name="Alice", username="alice", space="", last_used=0.0):
    return vault.TelegramAccount(
        tg_id=tg_id, name=name, username=username, phone="+100",
        space_id=space, last_used=last_used,
    )


def test_round_trip(tmp_path):
    p = _path(tmp_path)
    vault.save_all(p, [_a(1, "Alice"), _a(2, "Bob", "bob")])
    assert {a.tg_id for a in vault.load(p)} == {1, 2}


def test_missing_vault_is_empty_not_an_error(tmp_path):
    assert vault.load(_path(tmp_path)) == []


def test_corrupt_vault_degrades_instead_of_crashing(tmp_path):
    p = _path(tmp_path)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("{ not json")
    assert vault.load(p) == []


def test_entries_without_an_id_are_skipped(tmp_path):
    """An entry with no id names no session file, so it is not an account."""
    p = _path(tmp_path)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"accounts": [{"name": "nameless"},
                                {"tg_id": 7, "name": "real"}]}, fh)
    assert [a.tg_id for a in vault.load(p)] == [7]


def test_most_recently_used_comes_first(tmp_path):
    p = _path(tmp_path)
    vault.save_all(p, [_a(1, last_used=1.0), _a(2, "Bob", "bob", last_used=9.0)])
    assert vault.load(p)[0].tg_id == 2


def test_upsert_updates_in_place_and_keeps_the_others(tmp_path):
    p = _path(tmp_path)
    vault.upsert(p, _a(1, "Alice"))
    vault.upsert(p, _a(2, "Bob", "bob"))
    vault.upsert(p, _a(1, "Alice Renamed"))

    by_id = {a.tg_id: a for a in vault.load(p)}
    assert set(by_id) == {1, 2}
    assert by_id[1].name == "Alice Renamed"


def test_remove_returns_the_entry(tmp_path):
    p = _path(tmp_path)
    vault.save_all(p, [_a(1), _a(2, "Bob", "bob")])
    assert vault.remove(p, 1).tg_id == 1
    assert [a.tg_id for a in vault.load(p)] == [2]


def test_bind_and_unbind_a_space(tmp_path):
    p = _path(tmp_path)
    vault.save_all(p, [_a(1)])

    bound = vault.bind_space(p, 1, "!space:hs")
    assert bound.space_id == "!space:hs"
    assert vault.load(p)[0].space_id == "!space:hs"

    vault.bind_space(p, 1, "")
    assert vault.load(p)[0].space_id == ""


def test_find_by_id_username_name_and_position(tmp_path):
    p = _path(tmp_path)
    vault.save_all(p, [_a(1001, "Alice", "alice", last_used=9.0),
                       _a(2002, "Bob Smith", "bob")])
    assert vault.find(p, "1001").tg_id == 1001
    assert vault.find(p, "@bob").tg_id == 2002
    assert vault.find(p, "Bob Smith").tg_id == 2002
    assert vault.find(p, "2").tg_id == 2002        # 1-based, as printed
    assert vault.find(p, "nobody") is None


def test_an_exact_id_beats_a_position_guess(tmp_path):
    """Internal lookups pass str(tg_id); a short id must resolve to ITS
    account, never be misread as a printed list position."""
    p = _path(tmp_path)
    vault.upsert(p, _a(7, "Lucky", "lucky"), now=2.0)
    vault.upsert(p, _a(1001, "Alice", "alice"), now=1.0)
    assert vault.find(p, "7").tg_id == 7


def test_a_long_number_is_an_id_not_a_position(tmp_path):
    """Telegram ids are far longer than any list of accounts."""
    p = _path(tmp_path)
    vault.save_all(p, [_a(1001, "Alice"), _a(2002, "Bob", "bob")])
    assert vault.find(p, "2002").tg_id == 2002


def test_label_prefers_name_and_handle():
    assert _a(1, "Alice", "alice").label == "Alice (@alice)"
    assert _a(1, "Alice", "").label == "Alice"
    assert _a(1, "", "alice").label == "@alice"
    assert _a(1, "", "").label == "1"


def test_vault_file_is_not_world_readable(tmp_path):
    p = _path(tmp_path)
    vault.save_all(p, [_a()])
    mode = os.stat(p).st_mode & 0o077
    assert mode == 0 or os.name == "nt"  # NTFS ignores POSIX bits


# -- per-account directories ---------------------------------------------------


def test_each_account_gets_its_own_directory(tmp_path):
    d = str(tmp_path)
    assert vault.account_dir(d, 1) != vault.account_dir(d, 2)
    assert vault.slug(1001) == "tg-1001"


def test_session_lives_inside_the_account_directory(tmp_path):
    d = str(tmp_path)
    assert vault.session_path(d, 7).startswith(vault.account_dir(d, 7))


def test_two_accounts_cannot_share_a_session(tmp_path):
    d = str(tmp_path)
    assert vault.session_path(d, 1) != vault.session_path(d, 2)


# -- adopting a pre-multi-account install --------------------------------------


def test_legacy_caches_at_the_root_are_adopted(tmp_path):
    """Losing the room map would re-create every room in the Space."""
    d = str(tmp_path)
    with open(os.path.join(d, "rooms.json"), "w", encoding="utf-8") as fh:
        json.dump({"rooms": {"111": "!r:hs"}}, fh)

    moved = vault.adopt_legacy_caches(d, 7)

    with open(os.path.join(vault.account_dir(d, 7), "rooms.json"), encoding="utf-8") as fh:
        assert json.load(fh)["rooms"] == {"111": "!r:hs"}
    assert "rooms.json" in moved
    assert not os.path.exists(os.path.join(d, "rooms.json"))  # no second copy


def test_caches_from_the_matrix_named_layout_are_adopted(tmp_path):
    """A short-lived layout filed them under the Matrix account's name."""
    d = str(tmp_path)
    old = os.path.join(d, "accounts", "alice_matrix.org")
    os.makedirs(old)
    with open(os.path.join(old, "msglinks.json"), "w", encoding="utf-8") as fh:
        fh.write("[]")

    vault.adopt_legacy_caches(d, 7)

    assert os.path.exists(os.path.join(vault.account_dir(d, 7), "msglinks.json"))


def test_adoption_never_overwrites_an_account_that_already_has_data(tmp_path):
    d = str(tmp_path)
    target = vault.account_dir(d, 7)
    os.makedirs(target)
    with open(os.path.join(target, "rooms.json"), "w", encoding="utf-8") as fh:
        fh.write('{"rooms": {"999": "!keep:hs"}}')
    with open(os.path.join(d, "rooms.json"), "w", encoding="utf-8") as fh:
        fh.write('{"rooms": {"111": "!stale:hs"}}')

    vault.adopt_legacy_caches(d, 7)

    with open(os.path.join(target, "rooms.json"), encoding="utf-8") as fh:
        assert "999" in fh.read()


def test_adoption_ignores_other_accounts_directories(tmp_path):
    """Another Telegram account's caches are not leftovers."""
    d = str(tmp_path)
    other = vault.account_dir(d, 42)
    os.makedirs(other)
    with open(os.path.join(other, "rooms.json"), "w", encoding="utf-8") as fh:
        fh.write("{}")

    vault.adopt_legacy_caches(d, 7)

    assert os.path.exists(os.path.join(other, "rooms.json"))
    assert not os.path.exists(os.path.join(vault.account_dir(d, 7), "rooms.json"))


def test_adoption_is_idempotent(tmp_path):
    d = str(tmp_path)
    with open(os.path.join(d, "rooms.json"), "w", encoding="utf-8") as fh:
        fh.write("{}")
    assert vault.adopt_legacy_caches(d, 7) == ["rooms.json"]
    assert vault.adopt_legacy_caches(d, 7) == []


# -- signing in again must not reset the account ----------------------------


def test_install_session_keeps_the_accounts_caches(tmp_path):
    """Re-login after a revoked session is a repair, not a fresh start."""
    data = str(tmp_path)
    target = vault.account_dir(data, 777)
    os.makedirs(target)
    for name, body in (("rooms.json", '{"rooms": {"111": "!r:hs"}}'),
                       ("msglinks.json", "[]"),
                       ("state.json", '{"watched": ["-100333"]}')):
        with open(os.path.join(target, name), "w", encoding="utf-8") as fh:
            fh.write(body)
    with open(os.path.join(target, vault.SESSION_FILENAME), "w") as fh:
        fh.write("old session")

    scratch = os.path.join(data, "accounts", ".pending")
    os.makedirs(scratch)
    with open(os.path.join(scratch, vault.SESSION_FILENAME), "w") as fh:
        fh.write("new session")

    vault.install_session(scratch, data, 777)

    assert open(os.path.join(target, vault.SESSION_FILENAME)).read() == "new session"
    for kept in ("rooms.json", "msglinks.json", "state.json"):
        assert os.path.exists(os.path.join(target, kept)), kept
    assert not os.path.exists(scratch)


def test_install_session_clears_stale_sqlite_side_files(tmp_path):
    """A journal recovered against a new database corrupts the session."""
    data = str(tmp_path)
    target = vault.account_dir(data, 777)
    os.makedirs(target)
    with open(os.path.join(target, vault.SESSION_FILENAME + "-journal"), "w") as fh:
        fh.write("stale journal from the old session")

    scratch = os.path.join(data, "accounts", ".pending")
    os.makedirs(scratch)
    with open(os.path.join(scratch, vault.SESSION_FILENAME), "w") as fh:
        fh.write("new session")

    vault.install_session(scratch, data, 777)
    assert not os.path.exists(
        os.path.join(target, vault.SESSION_FILENAME + "-journal")
    )


def test_install_session_creates_a_first_time_account_dir(tmp_path):
    data = str(tmp_path)
    scratch = os.path.join(data, "accounts", ".pending")
    os.makedirs(scratch)
    with open(os.path.join(scratch, vault.SESSION_FILENAME), "w") as fh:
        fh.write("s")
    vault.install_session(scratch, data, 42)
    assert os.path.exists(vault.session_path(data, 42))
