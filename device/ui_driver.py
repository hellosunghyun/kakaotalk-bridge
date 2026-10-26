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

import json
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
        self.kind = kind
        previous = self._current_ime()
        self.started = self.clock()
        self.deadline = self.started + self.timeout
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
        except TimeoutError as exc:
            self._log(str(exc.args[0]) if exc.args else "unknown", ok=False)
            raise RuntimeError("ui_action_timeout") from None
        finally:
            self._delete_dumps()
            self._main_screen()
            self._restore_ime(previous)

    def _log(self, step, ok=True, **extra):
        """Step name and timing only. Never node text, which can hold message content."""
        record = {
            "ui_step": step,
            "kind": getattr(self, "kind", None),
            "ok": ok,
            "elapsed_ms": int((self.clock() - getattr(self, "started", self.clock())) * 1000),
        }
        record.update(extra)
        print(json.dumps(record), flush=True)

    def _wait(self, predicate, step="wait"):
        started = self.clock()
        while True:
            nodes = _nodes(self._dump())
            found = predicate(nodes)
            if found:
                self._log(step, step_ms=int((self.clock() - started) * 1000))
                return found
            if self.clock() >= self.deadline:
                raise TimeoutError(step)
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
            self._log("room_open_explicit")
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
        self._wait(lambda nodes: _composer(nodes), "room_open_deeplink")

    def _quote(self, item):
        preview = (item.get("reply_preview") or "").strip()
        text = item.get("text") or ""
        if not preview or not text.strip():
            raise RuntimeError("ui_action_failed")
        bubble = self._wait(
            lambda nodes: _lowest(nodes, lambda n: preview in n["text"]), "quote_bubble"
        )
        self._long_press(bubble)
        self._tap(self._wait(lambda nodes: _first(nodes, lambda n: n["text"] == "답장"), "quote_menu"))
        self._tap(self._wait(lambda nodes: _composer(nodes), "quote_composer"))
        self._enable_keyboard()
        self.commit_text(text)
        self._tap(self._wait(lambda nodes: _send(nodes), "quote_send"))

    def _emoticon(self, item):
        """26.9.1 tablet: the panel keeps its open state between rooms, so open it only if closed.

        Grid cells are `com.kakao.talk.emoticon:id/emoticon_icon` under the `즐겨찾기` header of the
        `최근사용 이모티콘 및 즐겨찾기` tab. Tapping a cell opens a preview (`emoticon_preview_root`);
        the composer's `send_button_layout` sends it.
        """
        index = item.get("emoticon_index")
        if not isinstance(index, int) or not 0 <= index <= 29:
            raise RuntimeError("ui_action_failed")
        nodes = _nodes(self._dump())
        if not _emoticon_panel(nodes):
            self._tap(self._wait(_emoticon_toggle, "emoticon_button"))
            nodes = self._wait(lambda found: found if _emoticon_panel(found) else None, "emoticon_panel")
        tab = _first(nodes, lambda n: n["desc"].endswith("이모티콘, 탭, 4개 중 2번째") or n["desc"] == "이모티콘, 탭")
        if tab and not tab["desc"].startswith("선택됨"):
            self._tap(tab)
        self._tap(self._wait(_favorites_tab, "emoticon_favorites_tab"))
        cells = self._wait(lambda found: _favorite_cells(found) or None, "emoticon_favorites")
        if index >= len(cells):
            self._log("emoticon_index_not_visible", ok=False, visible=len(cells))
            raise RuntimeError("ui_action_failed")
        self._tap(cells[index])
        self._wait(_emoticon_preview, "emoticon_preview")
        self._tap(self._wait(lambda nodes: _send(nodes), "emoticon_send"))
        self._wait(lambda nodes: None if _emoticon_preview(nodes) else (nodes or None), "emoticon_sent")

    def _location(self, item):
        """26.9.1: `media_send_layout` opens a sheet with `지도`; KakaoMapChatPlusWebActivity loads a
        web map. A "위치서비스" dialog may appear first and is dismissed with `취소`. The search
        field is the web node `검색어를 입력하세요`; Enter lists results, each with `이 장소 보내기`.
        The first visible result is sent and the room composer comes back.
        """
        location = item.get("location") or {}
        label = location.get("address") or location.get("title")
        if not label:
            lat, lng = location.get("latitude"), location.get("longitude")
            if lat is None or lng is None:
                raise RuntimeError("ui_action_failed")
            label = f"{lat},{lng}"
        self._tap(self._wait(_media_sheet_button, "location_media_button"))
        self._tap(self._wait(_map_entry, "location_map_entry"))
        search = self._wait(_map_search_or_dialog, "location_map_loaded")
        if search["text"] == "취소":
            self._tap(search)
            search = self._wait(_map_search, "location_map_search")
        self._tap(search)
        self._enable_keyboard()
        self.commit_text(str(label)[:120])
        self.adb("shell", "input", "keyevent", "66")
        self._tap(self._wait(_place_send, "location_result"))
        self._wait(lambda nodes: _composer(nodes), "location_sent")

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


def _emoticon_toggle(nodes):
    return _first(
        nodes,
        lambda n: n["rid"].endswith((":id/emoticon_button", ":id/emoticon_button_layout"))
        or n["desc"] == "이모티콘 키보드 열기"
        or n["text"] == "이모티콘",
    )


def _emoticon_panel(nodes):
    return _first(
        nodes,
        lambda n: n["rid"].endswith((":id/emoticon_grid", ":id/keyboard_tab_bar"))
        or n["desc"] == "이모티콘 키보드 닫기",
    )


def _favorites_tab(nodes):
    return _first(nodes, lambda n: n["desc"] == "최근사용 이모티콘 및 즐겨찾기")


def _favorite_cells(nodes):
    """Cells below the `즐겨찾기` header and above the next section header, in reading order."""
    headers = [n for n in nodes if n["rid"].endswith(":id/title") and n["text"]]
    start = next((n for n in headers if n["text"] == "즐겨찾기"), None)
    if start is None:
        return []
    below = [n["y"] for n in headers if n["y"] > start["y"]]
    end = min(below) if below else float("inf")
    cells = [
        n for n in nodes if n["rid"].endswith(":id/emoticon_icon") and start["y"] < n["y"] < end
    ]
    return sorted(cells, key=lambda node: (node["y"], node["x"]))


def _emoticon_preview(nodes):
    return _first(nodes, lambda n: n["rid"].endswith(":id/emoticon_preview_root"))


def _media_sheet_button(nodes):
    return _first(
        nodes,
        lambda n: n["rid"].endswith((":id/media_send_layout", "chat_plus_button"))
        or n["desc"] in {"미디어 전송 키보드 열기", "더보기", "첨부"},
    )


def _map_entry(nodes):
    return _first(nodes, lambda n: n["desc"] in {"지도", "위치"}) or _first(
        nodes, lambda n: n["rid"].endswith(":id/tv_label") and n["text"] in {"지도", "위치"}
    )


def _map_search(nodes):
    return _first(nodes, lambda n: n["text"] == "검색어를 입력하세요")


def _map_search_or_dialog(nodes):
    if any("위치서비스" in n["text"] for n in nodes):
        return _first(nodes, lambda n: n["text"] == "취소")
    return _map_search(nodes)


def _place_send(nodes):
    return _first(nodes, lambda n: n["text"] == "이 장소 보내기")


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
