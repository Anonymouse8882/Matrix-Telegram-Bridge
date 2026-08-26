"""Tiny shared JSON-list persistence for the background queues.

The outbound scheduler and the self-destruct expirer both keep a small list of
pending dicts on /data; the loading and atomic-write code is identical, so it
lives here once. A missing or corrupt file degrades to an empty queue — a bad
cache must never stop the bridge.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)


def load_json_list(path: str | None) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        log.exception("failed to load %s", path)
        return []


def save_json_list(path: str | None, items: list[dict], label: str = "queue") -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(items, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        log.exception("failed to save %s to %s", label, path)
