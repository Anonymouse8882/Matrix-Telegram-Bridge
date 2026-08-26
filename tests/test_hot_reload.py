import asyncio

import pytest

from bridge import creds as creds_store
from bridge.__main__ import _creds_fingerprint, _watch_creds


def _creds(user="@a:example.org", token="tok"):
    return creds_store.MatrixCreds(
        homeserver="https://example.org",
        user_id=user,
        access_token=token,
        device_id="DEV",
        control_room="!r:example.org",
    )


def test_fingerprint_of_missing_file_is_empty(tmp_path):
    assert _creds_fingerprint(str(tmp_path / "nope.json")) == ""


def test_fingerprint_changes_with_content(tmp_path):
    path = str(tmp_path / "creds.json")
    creds_store.save(path, _creds())
    first = _creds_fingerprint(path)

    creds_store.save(path, _creds(user="@b:example.org"))
    assert _creds_fingerprint(path) != first


def test_identical_rewrite_does_not_change_fingerprint(tmp_path):
    """Re-saving the same account must not churn the bridge into a reload."""
    path = str(tmp_path / "creds.json")
    creds_store.save(path, _creds())
    first = _creds_fingerprint(path)
    creds_store.save(path, _creds())
    assert _creds_fingerprint(path) == first


@pytest.fixture
def instant_sleep(monkeypatch):
    """Collapse the watcher's 2s poll interval so tests stay fast.

    Bind the real sleep first - patching asyncio.sleep with a lambda that
    calls asyncio.sleep would recurse into itself.
    """
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _delay: real_sleep(0))
    return real_sleep


@pytest.mark.asyncio
async def test_watcher_fires_when_credentials_appear(tmp_path, instant_sleep):
    path = str(tmp_path / "creds.json")
    changed = asyncio.Event()

    baseline = _creds_fingerprint(path)  # "" - no file yet
    task = asyncio.create_task(_watch_creds(path, baseline, changed))
    await instant_sleep(0)
    creds_store.save(path, _creds())

    await asyncio.wait_for(task, timeout=2)
    assert changed.is_set()


@pytest.mark.asyncio
async def test_watcher_stays_quiet_without_changes(tmp_path, instant_sleep):
    path = str(tmp_path / "creds.json")
    creds_store.save(path, _creds())
    changed = asyncio.Event()

    task = asyncio.create_task(
        _watch_creds(path, _creds_fingerprint(path), changed)
    )
    for _ in range(50):
        await instant_sleep(0)

    assert not changed.is_set()
    task.cancel()
