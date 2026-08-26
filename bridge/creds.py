"""Matrix credentials written by `bridge.mxlogin`, read by the bridge.

Why a file on the data volume instead of `.env`:

  * `.env` is read by docker-compose at *container create* time, so changing a
    token there needs `up --force-recreate`; a file on /data only needs a
    restart.
  * The token minted here belongs to a device the bridge itself created, so
    logging out of an Element session no longer kills the bridge (the failure
    mode that produced M_UNKNOWN_TOKEN).

Only ever holds one account — switching accounts overwrites it.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass
from typing import Optional

DEFAULT_FILENAME = "matrix_creds.json"


@dataclass
class MatrixCreds:
    homeserver: str
    user_id: str
    access_token: str
    device_id: str
    control_room: str = ""
    # True when the bridge minted this token itself (password login, its own
    # device). Only such a token may be revoked when the account is replaced —
    # an adopted one belongs to somebody's Element session, and logging that
    # out would kill a session the operator still uses. Absent in files written
    # before this existed, and "unknown" must mean "do not revoke".
    minted: bool = False


def load(path: str) -> Optional[MatrixCreds]:
    """Return stored credentials, or None if the file is absent/unusable.

    A corrupt file must not take the bridge down: the env/YAML values are still
    a valid fallback, so we degrade rather than raise.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(data, dict):
        return None
    required = ("homeserver", "user_id", "access_token")
    if not all(isinstance(data.get(k), str) and data.get(k) for k in required):
        return None

    return MatrixCreds(
        homeserver=data["homeserver"],
        user_id=data["user_id"],
        access_token=data["access_token"],
        device_id=str(data.get("device_id") or ""),
        control_room=str(data.get("control_room") or ""),
        minted=bool(data.get("minted", False)),
    )


def save(path: str, creds: MatrixCreds) -> None:
    """Write credentials atomically, owner-readable only."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(asdict(creds), fh, indent=2)
        fh.write("\n")
    _restrict(tmp)
    os.replace(tmp, path)  # atomic: readers never see a half-written token


def _restrict(path: str) -> None:
    """chmod 600 where the platform honours it (no-op on Windows/NTFS)."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - permission model varies by platform
        pass
