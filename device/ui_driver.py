"""On-device KakaoTalk UI actions. Default off (`KAKAO_UI_ACTIONS`).

Quote, favorite emoticon, and location have no exported send intent on 26.9.1.
This driver opens the room, taps through the UI, types with WebInputMethod, and
always returns to the KakaoTalk main screen. UI dumps contain message text, so
they are read into memory and deleted in the same step. Nothing here is written
to the KakaoTalk database.

Opening a room marks it read. That side effect is not reversible from here.

Room open (needs on-device verification on 26.9.1):

1. Explicit activity, using the same `key_id` extra the share intent uses, plus
   the historical `chatId` / `chatRoomId` extras::

       am start -n com.kakao.talk/com.kakao.talk.activity.chatroom.ChatRoomActivity \\
         --el key_id <chat_id> --el chatId <chat_id> --el chatRoomId <chat_id>

2. If the composer is not showing, a digits-only deeplink::

       am start -a android.intent.action.VIEW -d kakaotalk://chatroom/<chat_id>

The room counts as open only when the dump shows `message_edit_text` or
`send_button_layout`. Afterwards the launcher activity (AdaptiveMainActivity on
26.9.1) is started so the device is not left inside the room.
"""

import os
import re
import time
import xml.etree.ElementTree as ET

from device.screen import ensure_awake, screen_lock

DUMP = "/data/local/tmp/kb-ui-dump.xml"
DEFAULT_DUMP = "/sdcard/window_dump.xml"
ROOM_ACTIVITY = "com.kakao.talk/com.kakao.talk.activity.chatroom.ChatRoomActivity"
CHAT_ID = re.compile(r"^[1-9][0-9]{0,18}$")
BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
IME_ID = re.compile(r"^[A-Za-z0-9_.]+/[A-Za-z0-9_.]+$")


def ui_actions_enabled(environ=None):
    value = (environ if environ is not None else os.environ).get("KAKAO_UI_ACTIONS", "")
    return str(value).strip().lower() in {"1", "on", "true", "yes"}


def _center(bounds):
    match = BOUNDS.search(bounds or "")
    if not match:
        return None
    x1, y1, x2, y2 = (int(part) for part in match.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1 + x2) // 2, (y1 + y2) // 2, y2


def _nodes(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    found = []
    for node in root.iter("node"):
        center = _center(node.attrib.get("bounds", ""))
        if center is None:
            continue
        x, y, bottom = center
        found.append(
            {
                "text": node.attrib.get("text") or "",
                "desc": node.attrib.get("content-desc") or "",
                "rid": node.attrib.get("resource-id") or "",
                "x": x,
                "y": y,
                "bottom": bottom,
            }
        )
    return found


class UiDriver:
    def __init__(self, adb, commit_text, sleep=time.sleep, clock=time.monotonic, timeout=45):
        self.adb = adb
        self.commit_text = commit_text
        self.sleep = sleep
        self.clock = clock
        self.timeout = timeout

    def perform(self, item):
        if not ui_actions_enabled():
            raise RuntimeError("ui_actions_disabled")
        chat_id = str(item.get("chat_id") or "")
        if not CHAT_ID.fullmatch(chat_id):
            raise RuntimeError("ui_action_failed")
        kind = item.get("kind")
        previous = self._current_ime()
        self.deadline = self.clock() + self.timeout
        try:
            with screen_lock(timeout=self.timeout, clock=self.clock, sleep=self.sleep):
                ensure_awake(self.adb, sleep=self.sleep)
                self._open_room(chat_id)
                if kind == "reply":
                    self._quote(item)
                elif kind == "emoticon":
                    self._emoticon(item)
                elif kind == "location":
                    self._location(item)
                else:
                    raise RuntimeError("ui_action_failed")
        except TimeoutError:
            raise RuntimeError("ui_action_timeout") from None
        finally:
            self._delete_dumps()
            self._main_screen()
            self._restore_ime(previous)

    def _wait(self, predicate):
        while True:
            nodes = _nodes(self._dump())
            found = predicate(nodes)
            if found:
                return found
            if self.clock() >= self.deadline:
                raise TimeoutError("ui_action_timeout")
            self.sleep(0.4)

    def _dump(self):
        """Read a hierarchy into memory and delete the on-device file immediately."""
        try:
            self.adb("shell", "uiautomator", "dump", DUMP)
            return self.adb("shell", "cat", DUMP)
        finally:
            self._delete_dumps()

    def _delete_dumps(self):
        self.adb("shell", "rm", "-f", DUMP, check=False)
        self.adb("shell", "rm", "-f", DEFAULT_DUMP, check=False)

    def _tap(self, node):
        self.adb("shell", "input", "tap", str(node["x"]), str(node["y"]))

    def _long_press(self, node):
        x, y = str(node["x"]), str(node["y"])
        self.adb("shell", "input", "swipe", x, y, x, y, "800")

    def _open_room(self, chat_id):
        """Open by chat id. The explicit activity is tried first; the deeplink is the fallback."""
        self.adb(
            "shell",
            "am",
            "start",
            "-n",
            ROOM_ACTIVITY,
            "--el",
            "key_id",
            chat_id,
            "--el",
            "chatId",
            chat_id,
            "--el",
            "chatRoomId",
            chat_id,
        )
        self.sleep(0.3)
        if _composer(_nodes(self._dump())):
            return
        self.adb(
            "shell",
            "am",
            "start",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            f"kakaotalk://chatroom/{chat_id}",
        )
        self._wait(lambda nodes: _composer(nodes))

    def _quote(self, item):
        preview = (item.get("reply_preview") or "").strip()
        text = item.get("text") or ""
        if not preview or not text.strip():
            raise RuntimeError("ui_action_failed")
        bubble = self._wait(lambda nodes: _lowest(nodes, lambda n: preview in n["text"]))
        self._long_press(bubble)
        self._tap(self._wait(lambda nodes: _first(nodes, lambda n: n["text"] == "답장")))
        self._tap(self._wait(lambda nodes: _composer(nodes)))
        self._enable_keyboard()
        self.commit_text(text)
        self._tap(self._wait(lambda nodes: _send(nodes)))

    def _emoticon(self, item):
        index = item.get("emoticon_index")
        if not isinstance(index, int) or not 0 <= index <= 29:
            raise RuntimeError("ui_action_failed")
        self._tap(
            self._wait(
                lambda nodes: _first(
                    nodes,
                    lambda n: n["rid"].endswith("emoticon_button") or n["text"] == "이모티콘",
                )
            )
        )
        favorites = self._wait(
            lambda nodes: _first(nodes, lambda n: n["text"] in {"즐겨찾기", "이모티콘"})
        )
        self._tap(favorites)
        owned = self._wait(lambda nodes: _indexed(nodes, "emoticon_view", index))
        self._tap(owned)
        self._tap(self._wait(lambda nodes: _send(nodes)))

    def _location(self, item):
        location = item.get("location") or {}
        label = location.get("address") or location.get("title")
        if not label:
            lat, lng = location.get("latitude"), location.get("longitude")
            if lat is None or lng is None:
                raise RuntimeError("ui_action_failed")
            label = f"{lat},{lng}"
        self._tap(
            self._wait(
                lambda nodes: _first(
                    nodes,
                    lambda n: n["rid"].endswith("chat_plus_button") or n["desc"] in {"더보기", "첨부"},
                )
            )
        )
        self._tap(self._wait(lambda nodes: _first(nodes, lambda n: n["text"] in {"지도", "위치"})))
        self._enable_keyboard()
        self.commit_text(str(label)[:120])
        self._tap(self._wait(lambda nodes: _first(nodes, lambda n: n["text"] == "이 위치 보내기")))

    def _enable_keyboard(self):
        from device.cli import IME

        self.adb("shell", "ime", "enable", IME)
        self.adb("shell", "ime", "set", IME)

    def _current_ime(self):
        try:
            current = self.adb("shell", "settings", "get", "secure", "default_input_method")
        except (OSError, RuntimeError):
            return None
        current = (current or "").strip()
        return current if IME_ID.fullmatch(current) else None

    def _restore_ime(self, previous):
        if previous and IME_ID.fullmatch(previous):
            try:
                self.adb("shell", "ime", "set", previous, check=False)
            except (OSError, RuntimeError):
                pass

    def _main_screen(self):
        """KakaoTalk's launcher activity. Does not contain the room's message list."""
        try:
            resolved = self.adb(
                "shell",
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
                "com.kakao.talk",
            )
        except (OSError, RuntimeError):
            resolved = ""
        component = (resolved or "").strip().splitlines()[-1] if resolved else ""
        if re.fullmatch(r"com\.kakao\.talk/[A-Za-z0-9_.$]+", component):
            self.adb("shell", "am", "start", "-n", component, check=False)
        else:
            self.adb(
                "shell",
                "am",
                "start",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
                "-p",
                "com.kakao.talk",
                check=False,
            )


def _composer(nodes):
    return _first(nodes, lambda n: n["rid"].endswith(("message_edit_text", "send_button_layout")))


def _send(nodes):
    return _first(nodes, lambda n: n["rid"].endswith("send_button_layout") or n["text"] == "전송")


def _first(nodes, predicate):
    return next((node for node in nodes if predicate(node)), None)


def _lowest(nodes, predicate):
    matches = [node for node in nodes if predicate(node)]
    return max(matches, key=lambda node: node["bottom"]) if matches else None


def _indexed(nodes, suffix, index):
    matches = [node for node in nodes if node["rid"].endswith(suffix)]
    matches.sort(key=lambda node: (node["y"], node["x"]))
    return matches[index] if index < len(matches) else None
