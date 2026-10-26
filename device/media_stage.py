"""Stage outbound media where KakaoTalk's share intent reads it, then delete it.

Files live under `/sdcard/Android/data/com.kakao.talk/files` with a `kb-` prefix
(the Iris IMAGE_DIR). Only those names are removed. KakaoTalk's own files and
its database are left alone. Bytes are not logged.

KakaoTalk 26.9.1 queues a shared video (and can queue any share) in
`chat_sending_logs` with the `file://` URI and reads it later while it
transcodes and uploads. Deleting right after the intent left the video stuck
unsent, so a submitted or ambiguous share keeps its file for `PENDING_TTL`
seconds; the next stage sweeps only files older than that.
"""

import hashlib
import tempfile
from pathlib import Path

STAGE = "/sdcard/Android/data/com.kakao.talk/files"
PENDING_TTL = 30 * 60


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


def sweep(adb, keep_seconds=0):
    """Remove leftover `kb-` files. Nothing else.

    With `keep_seconds`, files modified more recently than that (by device clock)
    are kept, since KakaoTalk may still be reading them.
    """
    listing = adb("shell", "ls", "-1", STAGE, check=False) or ""
    now = None
    for name in listing.splitlines():
        name = name.strip()
        if not staged_name(name):
            continue
        if keep_seconds:
            if now is None:
                now = _int(adb("shell", "date", "+%s", check=False))
            mtime = _int(adb("shell", "stat", "-c", "%Y", _remote(name), check=False))
            if now is None or mtime is None or now - mtime < keep_seconds:
                continue
        adb("shell", "rm", "-f", _remote(name), check=False)


def _int(value):
    try:
        return int(str(value or "").strip())
    except ValueError:
        return None


def unstage(adb, names):
    for name in names:
        if staged_name(name):
            adb("shell", "rm", "-f", _remote(name), check=False)


def stage(adb, fetch, item):
    """Push claim attachments. `fetch(request_id, ordinal) -> bytes`. Returns staged names."""
    adb("shell", "mkdir", "-p", STAGE)
    sweep(adb, keep_seconds=PENDING_TTL)
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


def release_after_send(adb, names, keep):
    """A submitted or ambiguous share keeps its files for KakaoTalk; others are deleted now."""
    if not keep:
        unstage(adb, names)
