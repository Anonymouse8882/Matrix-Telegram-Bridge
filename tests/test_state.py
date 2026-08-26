import json

from bridge.core.state import BridgeState


def test_active_target_roundtrip():
    s = BridgeState()
    assert s.active_target("!r") is None
    s.set_active_target("!r", "123")
    assert s.active_target("!r") == "123"


def test_mute_unmute():
    s = BridgeState()
    assert not s.is_muted("42")
    s.mute("42")
    assert s.is_muted("42")
    assert s.muted() == {"42"}
    s.unmute("42")
    assert not s.is_muted("42")


def test_watch_unwatch():
    s = BridgeState()
    assert not s.is_watched("-100999")
    s.watch("-100999")
    assert s.is_watched("-100999")
    assert s.watched() == {"-100999"}
    s.unwatch("-100999")
    assert not s.is_watched("-100999")


def test_persists_and_reloads(tmp_path):
    path = str(tmp_path / "state.json")
    s = BridgeState(path)
    s.set_active_target("!r", "-100999")
    s.mute("7")
    s.watch("-100888")
    s.set_self_destruct("channel", 120)
    s.set_delay(5, 30)

    # A fresh instance reads the same file.
    s2 = BridgeState(path)
    assert s2.active_target("!r") == "-100999"
    assert s2.is_muted("7")
    assert s2.is_watched("-100888")
    assert s2.self_destruct("channel") == 120
    assert s2.delay_fixed() == 5 and s2.delay_random() == 30

    on_disk = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert on_disk["active"]["!r"] == "-100999"
    assert on_disk["muted"] == ["7"]
    assert on_disk["watched"] == ["-100888"]


def test_corrupt_state_does_not_crash(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ not json", encoding="utf-8")
    s = BridgeState(str(path))  # should log and start empty, not raise
    assert s.active_target("!r") is None


def test_state_is_written_atomically(tmp_path):
    """Writing in place truncates first; a crash mid-write would leave torn
    JSON, and _load turns that into "no settings at all"."""
    path = tmp_path / "state.json"
    state = BridgeState(str(path))
    state.watch("-100333")
    state.mute("222")

    assert not (tmp_path / "state.json.tmp").exists()   # renamed, not left behind
    assert json.loads(path.read_text(encoding="utf-8"))["watched"] == ["-100333"]


def test_a_torn_write_cannot_happen_mid_rename(tmp_path, monkeypatch):
    """The real file is only ever replaced by a complete temp file."""
    import os as _os

    path = tmp_path / "state.json"
    BridgeState(str(path)).watch("-100333")
    seen = {}

    real_replace = _os.replace

    def spy(src, dst):
        # Whatever is about to become state.json must already parse.
        seen["complete"] = json.loads(open(src, encoding="utf-8").read())
        return real_replace(src, dst)

    monkeypatch.setattr(_os, "replace", spy)
    BridgeState(str(path)).mute("222")
    assert seen["complete"]["watched"] == ["-100333"]
