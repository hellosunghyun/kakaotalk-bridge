"""One device-wide screen lock, plus wake/lock checks for sends that open KakaoTalk.

UI dumps and message text never belong in this module. The lock file is empty.
"""

import fcntl
import os
import time
from contextlib import contextmanager

LOCK_DEFAULT = "/tmp/kakaotalk-bridge-screen.lock"
LOCKED = ("mDreamingLockscreen=true", "isKeyguardShowing=true", "mShowingLockscreen=true")
UNLOCKED = ("mDreamingLockscreen=false", "isKeyguardShowing=false", "mShowingLockscreen=false")


class ScreenBusy(RuntimeError):
    def __init__(self):
        super().__init__("screen_busy")


def lock_path():
    return os.getenv("KAKAO_SCREEN_LOCK", LOCK_DEFAULT)


def lock_state(dump):
    """Android 14 `dumpsys window` markers. Locked wins if both appear."""
    text = dump or ""
    if any(marker in text for marker in LOCKED):
        return "locked"
    if any(marker in text for marker in UNLOCKED):
        return "unlocked"
    return "unknown"


def screen_off(dump):
    text = dump or ""
    return any(
        marker in text
        for marker in ("Display Power: state=OFF", "mWakefulness=Asleep", "mWakefulness=Dozing")
    )


@contextmanager
def screen_lock(timeout=45, clock=time.monotonic, sleep=time.sleep):
    """Exclusive across processes on this host (fcntl). One Android screen, one holder."""
    path = lock_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    deadline = clock() + timeout
    with open(path, "a+") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock() >= deadline:
                    raise ScreenBusy() from None
                sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def ensure_awake(adb, sleep=time.sleep):
    """Wake a dark screen. Refuse a lock screen instead of dismissing it."""
    window = adb("shell", "dumpsys", "window")
    state = lock_state(window)
    if state == "locked":
        raise RuntimeError("screen_locked")
    if state != "unlocked":
        raise RuntimeError("screen_state_unreadable")
    if screen_off(adb("shell", "dumpsys", "power")):
        adb("shell", "input", "keyevent", "224")
        sleep(0.4)
        if screen_off(adb("shell", "dumpsys", "power")):
            raise RuntimeError("screen_state_unreadable")
