"""Stage outbound media where KakaoTalk's share intent reads it, then delete it.

Files live under `/sdcard/Android/data/com.kakao.talk/files` with a `kb-` prefix
(the Iris IMAGE_DIR). Only those names are removed. KakaoTalk's own files and
its database are left alone. Bytes are not logged.
"""

import hashlib
import tempfile
import time
from pathlib import Path

STAGE = "/sdcard/Android/data/com.kakao.talk/files"


def staged_name(name):
    return (
        isinstance(name, str)
        and name.startswith("kb-")
        and len(name) <= 80
        and name not in {"kb-", "kb-."}
        and ".." not in name
        and "/" not in name
        and "\\" not in name
        and all(ord(ch) >= 32 for ch in name)
    )


def _remote(name):
    if not staged_name(name):
        raise RuntimeError("media_stage_failed")
    return f"{STAGE}/{name}"


def sweep(adb):
    """Remove leftover `kb-` files from an interrupted send. Nothing else."""
    listing = adb("shell", "ls", "-1", STAGE, check=False) or ""
    for name in listing.splitlines():
        name = name.strip()
        if staged_name(name):
            adb("shell", "rm", "-f", _remote(name), check=False)


def unstage(adb, names):
    for name in names:
        if staged_name(name):
            adb("shell", "rm", "-f", _remote(name), check=False)


def stage(adb, fetch, item):
    """Push claim attachments. `fetch(request_id, ordinal) -> bytes`. Returns staged names."""
    adb("shell", "mkdir", "-p", STAGE)
    sweep(adb)
    names = []
    try:
        for attachment in item.get("attachments") or []:
            name = attachment["name"]
            remote = _remote(name)
            data = fetch(item["request_id"], attachment["ordinal"])
            if (
                not isinstance(data, (bytes, bytearray))
                or hashlib.sha256(data).hexdigest() != attachment["sha256"]
                or len(data) != attachment["size"]
                or not data
            ):
                raise RuntimeError("media_stage_failed")
            with tempfile.TemporaryDirectory() as folder:
                local = Path(folder) / "blob"
                local.write_bytes(data)
                local.chmod(0o600)
                adb("push", str(local), remote)
            adb("shell", "chmod", "644", remote)
            names.append(name)
        return names
    except Exception:
        unstage(adb, names)
        raise


def release_after_send(adb, names, submitted, sleep=None):
    """Give KakaoTalk a moment to open a submitted share, then delete our files."""
    if submitted:
        (time.sleep if sleep is None else sleep)(2)
    unstage(adb, names)
