from bridge.core.replymap import ReplyMap, ReplyRef


def _ref(n):
    return ReplyRef(chat_id=n, msg_id=n * 10, kind="user", name=f"c{n}")


def test_remember_and_lookup():
    m = ReplyMap()
    m.remember("$evt1", _ref(1))
    got = m.lookup("$evt1")
    assert got.chat_id == 1 and got.msg_id == 10


def test_lookup_missing_or_none():
    m = ReplyMap()
    assert m.lookup("$nope") is None
    assert m.lookup(None) is None


def test_eviction_beyond_capacity():
    m = ReplyMap(capacity=2)
    m.remember("$a", _ref(1))
    m.remember("$b", _ref(2))
    m.remember("$c", _ref(3))  # evicts the oldest ($a)
    assert m.lookup("$a") is None
    assert m.lookup("$b") is not None
    assert m.lookup("$c") is not None
