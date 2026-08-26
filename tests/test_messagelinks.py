import os

from bridge.core.messagelinks import MessageLinks


def _links(tmp_path=None, **kw):
    path = str(tmp_path / "links.json") if tmp_path else None
    clock = kw.pop("clock", lambda: 1000.0)
    return MessageLinks(path=path, clock=clock, **kw)


def test_exact_lookup_by_chat_and_msg():
    ml = _links()
    ml.add(-100222, 5, "channel", "!r:hs", "$e1")
    link = ml.find(-100222, 5)
    assert link.room_id == "!r:hs" and link.event_id == "$e1"


def test_channel_msg_ids_are_scoped_to_their_chat():
    """Two channels can share a msg id; the chat id disambiguates."""
    ml = _links()
    ml.add(-100111, 5, "channel", "!a:hs", "$a")
    ml.add(-100222, 5, "channel", "!b:hs", "$b")
    assert ml.find(-100111, 5).event_id == "$a"
    assert ml.find(-100222, 5).event_id == "$b"


def test_get_is_exact_only():
    """`get` (reply targets) must never fall back across chats like `find`."""
    ml = _links()
    ml.add(111, 42, "user", "!dm:hs", "$dm")
    assert ml.get(111, 42).event_id == "$dm"
    assert ml.get(222, 42) is None


def test_dm_lookup_by_msg_id_only():
    """Telegram omits the chat for DM/basic-group deletions; ids are unique."""
    ml = _links()
    ml.add(111, 42, "user", "!dm:hs", "$dm")
    assert ml.find(None, 42).event_id == "$dm"


def test_msg_id_only_lookup_skips_channels():
    """A msg-id-only (DM) deletion must not match a channel message."""
    ml = _links()
    ml.add(-100222, 42, "channel", "!chan:hs", "$chan")
    assert ml.find(None, 42) is None


def test_unknown_returns_none():
    assert _links().find(1, 1) is None


def test_channel_delete_never_falls_back_to_another_chat():
    """A deletion in an unrelayed channel shares small msg ids with DMs; it
    must not strike through an unrelated DM message that has the same id."""
    ml = _links()
    ml.add(111, 42, "user", "!dm:hs", "$dm")
    assert ml.find(-100999, 42) is None


def test_dm_id_form_mismatch_still_resolves():
    """A DM/basic-group id in a different form keeps the msg-id fallback."""
    ml = _links()
    ml.add(111, 42, "user", "!dm:hs", "$dm")
    assert ml.find(999, 42).event_id == "$dm"


def test_forget_removes_the_link():
    ml = _links()
    ml.add(111, 42, "user", "!dm:hs", "$dm")
    ml.forget(111, 42)
    assert ml.find(111, 42) is None


def test_capacity_evicts_oldest_first():
    ml = _links(capacity=2)
    ml.add(1, 1, "user", "!r:hs", "$1")
    ml.add(2, 2, "user", "!r:hs", "$2")
    ml.add(3, 3, "user", "!r:hs", "$3")
    assert ml.find(1, 1) is None      # oldest evicted
    assert ml.find(3, 3) is not None


def test_age_prune_on_add():
    now = [1000.0]
    ml = _links(max_age_days=1, clock=lambda: now[0])
    ml.add(1, 1, "user", "!r:hs", "$old")
    now[0] += 2 * 86400  # two days later
    ml.add(2, 2, "user", "!r:hs", "$new")
    assert ml.find(1, 1) is None
    assert ml.find(2, 2) is not None


def test_persistence_round_trip(tmp_path):
    ml = _links(tmp_path)
    ml.add(-100222, 5, "channel", "!r:hs", "$e1")
    ml.add(111, 42, "user", "!dm:hs", "$dm")

    fresh = _links(tmp_path)
    assert fresh.find(-100222, 5).event_id == "$e1"
    assert fresh.find(None, 42).event_id == "$dm"


def test_corrupt_file_degrades_to_empty(tmp_path):
    path = tmp_path / "links.json"
    path.write_text("not json", encoding="utf-8")
    assert MessageLinks(str(path), clock=lambda: 1.0).find(1, 1) is None


def test_readd_refreshes_recency():
    ml = _links(capacity=2)
    ml.add(1, 1, "user", "!r:hs", "$1")
    ml.add(2, 2, "user", "!r:hs", "$2")
    ml.add(1, 1, "user", "!r:hs", "$1b")  # touch key 1 -> now freshest
    ml.add(3, 3, "user", "!r:hs", "$3")   # evicts key 2, not key 1
    assert ml.find(1, 1).event_id == "$1b"
    assert ml.find(2, 2) is None


# -- msg-id fallback must exclude channel PEERS, not kind == "channel" -------


def test_dm_delete_does_not_match_a_megagroup_with_the_same_msg_id():
    """A megagroup is stored as kind "group" but has its own id counter, so
    its ids collide with the account-wide sequence DMs draw from."""
    links = MessageLinks(clock=lambda: 1.0)
    links.add(111, 42, "user", "!dm:hs", "$dm")
    links.add(-1001234567890, 42, "group", "!group:hs", "$group")

    hit = links.find(None, 42)      # a DM delete: Telegram omits the peer
    assert hit is not None and hit.chat_id == 111 and hit.event_id == "$dm"


def test_basic_group_still_matches_the_msg_id_fallback():
    """Basic groups (a plain negative id) DO share the account-wide sequence."""
    links = MessageLinks(clock=lambda: 1.0)
    links.add(-333, 42, "group", "!basic:hs", "$basic")
    assert links.find(None, 42).event_id == "$basic"


# -- event-id reverse index --------------------------------------------------


def test_by_event_finds_the_telegram_message():
    links = MessageLinks(clock=lambda: 1.0)
    links.add(111, 42, "user", "!dm:hs", "$evt")
    link = links.by_event("$evt")
    assert (link.chat_id, link.msg_id) == (111, 42)


def test_by_event_forgets_with_the_link():
    links = MessageLinks(clock=lambda: 1.0)
    links.add(111, 42, "user", "!dm:hs", "$evt")
    links.forget(111, 42)
    assert links.by_event("$evt") is None


def test_by_event_survives_a_reload(tmp_path):
    path = str(tmp_path / "msglinks.json")
    MessageLinks(path, clock=lambda: 1.0).add(111, 42, "user", "!dm:hs", "$evt")
    assert MessageLinks(path, clock=lambda: 1.0).by_event("$evt").msg_id == 42


def test_by_event_is_dropped_on_eviction():
    links = MessageLinks(capacity=1, clock=lambda: 1.0)
    links.add(111, 1, "user", "!a:hs", "$old")
    links.add(111, 2, "user", "!b:hs", "$new")
    assert links.by_event("$old") is None
    assert links.by_event("$new") is not None


# -- a bad record must not cost the whole file -------------------------------


def test_one_unreadable_record_does_not_discard_the_rest(tmp_path):
    import json

    path = tmp_path / "msglinks.json"
    good = {"chat_id": 111, "msg_id": 1, "kind": "user", "room_id": "!r:hs",
            "event_id": "$e", "ts": 1.0, "text": "keep me", "head": "",
            "media": False, "orig": ""}
    path.write_text(json.dumps([
        good,
        {"chat_id": 222, "msg_id": 2},                    # missing fields
        "not even a dict",
        {**good, "msg_id": 3, "event_id": "$e3", "future_field": "?"},
    ]), encoding="utf-8")

    links = MessageLinks(str(path), clock=lambda: 1.0)
    assert links.get(111, 1).text == "keep me"            # survived
    assert links.get(111, 3) is not None                  # unknown key ignored


# -- close(): flush what is pending, and never write afterwards --------------


async def test_close_flushes_a_pending_debounced_write(tmp_path):
    path = str(tmp_path / "msglinks.json")
    links = MessageLinks(path, clock=lambda: 1.0, flush_delay=60.0)
    links.add(111, 42, "user", "!dm:hs", "$e", "would be lost on restart")
    assert not os.path.exists(path)      # still only scheduled

    links.close()
    assert MessageLinks(path, clock=lambda: 1.0).get(111, 42) is not None


async def test_close_stops_the_timer_recreating_a_wiped_directory(tmp_path):
    import asyncio
    import shutil

    account_dir = tmp_path / "accounts" / "tg-1"
    account_dir.mkdir(parents=True)
    links = MessageLinks(str(account_dir / "msglinks.json"), clock=lambda: 1.0)
    links.add(111, 42, "user", "!dm:hs", "$e", "private message text")

    links.close()                        # AccountRuntime.stop() does this...
    shutil.rmtree(account_dir)           # ...before logout wipes the account

    await asyncio.sleep(1.2)             # past the debounce deadline
    assert not account_dir.exists()      # nothing resurrected it


async def test_a_link_added_while_closing_is_still_persisted(tmp_path):
    """AccountRuntime.stop() closes the source first, so a message still being
    drained can record its link before the writer is disarmed."""
    path = str(tmp_path / "msglinks.json")
    links = MessageLinks(path, clock=lambda: 1.0, flush_delay=60.0)
    links.add(111, 1, "user", "!dm:hs", "$early")
    links.add(111, 2, "user", "!dm:hs", "$late")   # drained during source.close()
    links.close()

    reloaded = MessageLinks(path, clock=lambda: 1.0)
    assert reloaded.get(111, 1) is not None
    assert reloaded.get(111, 2) is not None
