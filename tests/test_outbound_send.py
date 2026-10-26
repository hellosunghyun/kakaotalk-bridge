"""API, worker, and UI-driver coverage for media and flagged KakaoTalk actions."""

import base64
import hashlib
import json
import multiprocessing
import os
import time
from uuid import uuid4

import pytest

from device import iris
from device.media_stage import STAGE, staged_name, sweep
from device.screen import ScreenBusy, lock_state, screen_lock, screen_off
from device.ui_driver import ROOM_ACTIVITY, UiDriver
from server.outgoing import OutgoingRequest
from tests.test_api import INGEST, auth
from tests.test_iris import CONFIG, row
from tests.test_outgoing import enqueue, pipeline, sending, status

# Re-export fixtures so pytest collects them in this module.
_ = (pipeline, sending)

CHAT = "9007199254740993"
PREVIEW = "알림을 끈 방의 메시지"


def jpeg(size=32):
    raw = b"\xff\xd8\xff" + b"x" * size
    return raw, base64.b64encode(raw).decode()


def media_body(sending_fixture, **extra):
    _collector, _client, _page, body = sending_fixture
    raw, encoded = jpeg()
    payload = {
        "request_id": str(uuid4()),
        "conversation_ref": body["conversation_ref"],
        "attachments": [{"name": "shot.jpg", "mime": "image/jpeg", "data_base64": encoded}],
    }
    return raw, payload | extra


def claim(client, collector):
    return client.post(
        "/internal/v1/outgoing/claim",
        headers=auth(INGEST),
        json={
            "enrollment_epoch": collector.epoch,
            "account_ref": collector.account_ref,
            "database_id": collector.database_id,
        },
    ).json()


def finish(client, request_id, **extra):
    return client.post(
        f"/internal/v1/outgoing/{request_id}/result",
        headers=auth(INGEST),
        json={"status": "submitted", **extra},
    )


def test_plain_text_and_thread_id_keep_distinct_digests(sending):
    collector, client, _, body = sending
    ack = body | {"request_id": str(uuid4()), "text": "💭 Grok Bot"}
    assert enqueue(client, ack).status_code == 200
    threaded = body | {"request_id": str(uuid4()), "thread_id": "42"}
    assert enqueue(client, threaded).json()["kind"] == "text"
    assert claim(client, collector)["item"]["text"] == "💭 Grok Bot"
    assert claim(client, collector)["item"]["thread_id"] == "42"
    assert enqueue(client, threaded | {"thread_id": "43"}).status_code == 409
    assert enqueue(client, threaded).json()["status"] == "dispatching"


@pytest.mark.parametrize(
    "extra",
    [
        {"attachments": [{"name": "a.exe", "mime": "image/jpeg", "data_base64": "aaaa"}]},
        {
            "attachments": [
                {"name": "shot.jpg", "mime": "application/x-msdownload", "data_base64": "aaaa"}
            ]
        },
        {
            "attachments": [
                {"name": "a.jpg", "mime": "image/jpeg", "data_base64": "aaaa"},
                {"name": "b.mp4", "mime": "video/mp4", "data_base64": "aaaa"},
            ]
        },
        {"text": "hi", "attachments": [{"name": "a.jpg", "mime": "image/jpeg", "data_base64": "aaaa"}]},
        {
            "thread_id": "9",
            "attachments": [{"name": "a.jpg", "mime": "image/jpeg", "data_base64": "aaaa"}],
        },
        {"text": "hi", "reply_to": "10", "thread_id": "9"},
        {"latitude": 37.5},
        {"favorite_index": 40},
    ],
)
def test_outgoing_rejects_invalid_media_and_mixed_modes(sending, extra):
    _, client, _, body = sending
    payload = {"request_id": str(uuid4()), "conversation_ref": body["conversation_ref"], **extra}
    if "text" not in extra and "attachments" not in extra and "reply_to" not in extra:
        payload.pop("text", None)
    response = enqueue(client, payload)
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request"}


def test_path_bytes_are_reduced_to_a_safe_staged_name():
    raw = b"\xff\xd8\xffhello"
    body = OutgoingRequest.model_validate(
        {
            "request_id": str(uuid4()),
            "conversation_ref": "room",
            "attachments": [
                {
                    "name": "folder/사진.jpg",
                    "mime": "image/jpeg",
                    "data_base64": base64.b64encode(raw).decode(),
                }
            ],
        }
    )
    assert body.files[0]["name"] == "kb-0-사진.jpg"
    assert not staged_name("../kb-0-사진.jpg")
    assert not staged_name("kb-0-사진.jpg/../../x")
    assert staged_name("kb-0-사진.jpg")


def test_oversize_attachment_is_rejected(sending, monkeypatch):
    monkeypatch.setattr("server.outgoing.MAX_ATTACHMENT_BYTES", 8)
    _, client, _, body = sending
    raw, encoded = jpeg(size=20)
    assert len(raw) > 8
    response = enqueue(
        client,
        {
            "request_id": str(uuid4()),
            "conversation_ref": body["conversation_ref"],
            "attachments": [{"name": "shot.jpg", "mime": "image/jpeg", "data_base64": encoded}],
        },
    )
    assert response.status_code == 422


def test_ui_actions_stay_off_until_the_flag_and_a_visible_source_exist(sending, monkeypatch):
    collector, client, page, body = sending
    monkeypatch.delenv("KAKAO_UI_ACTIONS", raising=False)
    reply = body | {"reply_to": "10"}
    denied = enqueue(client, reply)
    assert denied.status_code == 403
    assert denied.json() == {"error": "ui_actions_disabled"}
    assert (
        client.get("/v1/outgoing/" + reply["request_id"], headers=auth("s" * 64)).status_code
        == 404
    )
    emoticon = {
        "request_id": str(uuid4()),
        "conversation_ref": body["conversation_ref"],
        "favorite_index": 0,
    }
    assert enqueue(client, emoticon).status_code == 403
    monkeypatch.setenv("KAKAO_UI_ACTIONS", "on")
    assert enqueue(client, reply).status_code == 200
    assert PREVIEW not in enqueue(client, reply).text
    listed = client.get("/v2/messages", headers=auth()).json()["items"]
    assert any(item["log_id"] == "10" for item in listed)
    item = claim(client, collector)["item"]
    assert item["kind"] == "reply"
    assert item["reply_preview"] == PREVIEW
    assert item["text"] == body["text"]
    monkeypatch.delenv("KAKAO_UI_ACTIONS", raising=False)
    assert enqueue(client, reply).json()["status"] == "dispatching"
    page["rows"] = [row(20, message="  \n")]
    page["high_water"] = "20"
    collector.tick()
    blank = body | {"request_id": str(uuid4()), "reply_to": "20"}
    monkeypatch.setenv("KAKAO_UI_ACTIONS", "yes")
    assert enqueue(client, blank).json() == {"error": "reply_source_has_no_visible_text"}
    other = body | {"request_id": str(uuid4()), "reply_to": "999"}
    assert enqueue(client, other).json() == {"error": "reply_source_not_found"}


def test_location_accepts_integer_coordinates_only_when_enabled(sending, monkeypatch):
    _, client, _, body = sending
    monkeypatch.delenv("KAKAO_UI_ACTIONS", raising=False)
    payload = {
        "request_id": str(uuid4()),
        "conversation_ref": body["conversation_ref"],
        "latitude": 37,
        "longitude": 127,
        "location_title": "집",
    }
    assert enqueue(client, payload).status_code == 403
    monkeypatch.setenv("KAKAO_UI_ACTIONS", "true")
    queued = enqueue(client, payload)
    assert queued.status_code == 200
    assert queued.json()["kind"] == "location"
    assert "집" not in queued.text


def test_delivery_matches_the_earliest_later_mine_row_once(sending, monkeypatch):
    collector, client, page, body = sending
    first = body | {"request_id": str(uuid4())}
    second = body | {"request_id": str(uuid4()), "text": "둘째"}
    enqueue(client, first)
    enqueue(client, second)

    original = iris.request

    def submit(url, payload=None, token=None):
        if str(url).endswith("/collector/send"):
            return {**payload, "status": "submitted", "build": iris.BUILD, "high_water": "11"}
        return original(url, payload, token)

    monkeypatch.setattr(iris, "request", submit)
    collector.send_pending(CONFIG)
    collector.send_pending(CONFIG)
    page["rows"] = [
        row(12, is_mine=False, message_type="1"),
        row(13, is_mine=True, message_type="2", message="photo"),
        row(14, is_mine=True, message_type="1", message="mine-1"),
    ]
    page["high_water"] = "14"
    collector.tick()
    matched = status(client, first)
    assert matched["delivery_confirmed"] is True
    assert matched["reason"] == "matched_collected_message"
    assert matched["matched_message_id"]
    waiting = status(client, second)
    assert waiting["delivery_confirmed"] is False
    page["rows"] = [row(15, is_mine=True, message_type="1", message="mine-2")]
    page["high_water"] = "15"
    collector.tick()
    assert status(client, second)["delivery_confirmed"] is True
    assert status(client, second)["matched_message_id"] != matched["matched_message_id"]


def test_media_blob_is_readable_only_while_dispatching(sending):
    collector, client, _, body = sending
    raw, payload = media_body((collector, client, None, body))
    assert enqueue(client, payload).json()["kind"] == "media"
    item = claim(client, collector)["item"]
    assert item["attachments"][0]["name"] == "kb-0-shot.jpg"
    assert "bytes" not in item["attachments"][0]
    assert item["attachments"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
    fetched = client.get(
        f"/internal/v1/outgoing/{payload['request_id']}/files/0", headers=auth(INGEST)
    )
    assert fetched.status_code == 200
    assert fetched.content == raw
    assert fetched.headers["content-type"].startswith("application/octet-stream")
    assert "content-disposition" not in {key.lower() for key in fetched.headers}
    assert (
        client.get(
            f"/internal/v1/outgoing/{payload['request_id']}/files/0", headers=auth("s" * 64)
        ).status_code
        == 401
    )
    finish(client, payload["request_id"], match_after_seq=11)
    assert (
        client.get(
            f"/internal/v1/outgoing/{payload['request_id']}/files/0", headers=auth(INGEST)
        ).status_code
        == 404
    )


def _adb_script(window="mDreamingLockscreen=false", power="Display Power: state=ON", listing=""):
    calls = []

    def adb(*args, check=True, timeout=25):
        calls.append(args)
        if args[:3] == ("shell", "dumpsys", "window"):
            return window
        if args[:3] == ("shell", "dumpsys", "power"):
            return power
        if args[:2] == ("shell", "ls"):
            return listing
        return ""

    return calls, adb


def test_media_worker_sends_names_and_keeps_submitted_files(sending, monkeypatch, tmp_path):
    monkeypatch.setenv("KAKAO_SCREEN_LOCK", str(tmp_path / "screen.lock"))
    collector, client, _, body = sending
    raw, payload = media_body((collector, client, None, body))
    enqueue(client, payload)
    calls, adb = _adb_script()
    sent = []
    monkeypatch.setattr(iris.cli, "adb", adb)
    monkeypatch.setattr(collector, "_fetch_blob", lambda request_id, ordinal: raw)

    def request(url, payload=None, token=None):
        sent.append(payload)
        assert raw not in json.dumps(payload).encode()
        assert payload["attachments"] == [{"name": "kb-0-shot.jpg", "kind": "image"}]
        return {**payload, "status": "submitted", "build": iris.BUILD, "high_water": "11"}

    monkeypatch.setattr(iris, "request", request)
    collector.send_pending(CONFIG)
    assert sent[0]["attachments"][0]["name"].startswith("kb-")
    # KakaoTalk may still read a submitted share; the file stays for the next aged sweep.
    assert not any(call[:3] == ("shell", "rm", "-f") and call[-1].endswith("kb-0-shot.jpg") for call in calls)
    assert status(client, payload)["status"] == "submitted"
    assert status(client, payload)["delivery_confirmed"] is False


def test_locked_screen_does_not_call_kakao(sending, monkeypatch, tmp_path):
    monkeypatch.setenv("KAKAO_SCREEN_LOCK", str(tmp_path / "screen.lock"))
    collector, client, _, body = sending
    _raw, payload = media_body((collector, client, None, body))
    enqueue(client, payload)
    calls, adb = _adb_script(window="isKeyguardShowing=true")
    monkeypatch.setattr(iris.cli, "adb", adb)
    monkeypatch.setattr(iris, "request", lambda *args, **kwargs: pytest.fail("must not send"))
    collector.send_pending(CONFIG)
    assert status(client, payload)["status"] == "failed"
    assert status(client, payload)["reason"] == "screen_locked"
    assert not any(call[0] == "push" for call in calls)


def test_sweep_removes_only_bridge_names():
    removed = []

    def adb(*args, check=True, timeout=25):
        if args[1] == "ls":
            return "kb-0-a.jpg\nKakaoTalk.jpg\nkb-../x\n"
        removed.append(args)
        return ""

    sweep(adb)
    assert removed == [("shell", "rm", "-f", f"{STAGE}/kb-0-a.jpg")]


def _hold_lock(path, held, release):
    os.environ["KAKAO_SCREEN_LOCK"] = path
    with screen_lock(timeout=3):
        with open(held, "w", encoding="utf-8") as handle:
            handle.write("1")
        for _ in range(200):
            if os.path.exists(release):
                return
            time.sleep(0.02)


def test_screen_markers_and_exclusive_lock(tmp_path, monkeypatch):
    assert lock_state("isKeyguardShowing=true mDreamingLockscreen=false") == "locked"
    assert lock_state("mShowingLockscreen=false") == "unlocked"
    assert lock_state("") == "unknown"
    assert screen_off("mWakefulness=Dozing")
    path = str(tmp_path / "screen.lock")
    monkeypatch.setenv("KAKAO_SCREEN_LOCK", path)
    held = str(tmp_path / "held")
    release = str(tmp_path / "release")
    proc = multiprocessing.Process(target=_hold_lock, args=(path, held, release))
    proc.start()
    for _ in range(100):
        if os.path.exists(held):
            break
        time.sleep(0.02)
    assert os.path.exists(held)
    with pytest.raises(ScreenBusy), screen_lock(timeout=0.15):
        pass
    os.close(os.open(release, os.O_CREAT | os.O_WRONLY, 0o644))
    proc.join(3)
    assert proc.exitcode == 0


def _xml(**attrs):
    body = "".join(f' {key}="{value}"' for key, value in attrs.items())
    return f"<node{body} />"


def hierarchy(*nodes):
    return "<?xml version='1.0' encoding='UTF-8' ?><hierarchy>" + "".join(nodes) + "</hierarchy>"


def test_quote_driver_uses_separate_argv_and_deletes_dumps(tmp_path, monkeypatch):
    monkeypatch.setenv("KAKAO_UI_ACTIONS", "on")
    monkeypatch.setenv("KAKAO_SCREEN_LOCK", str(tmp_path / "screen.lock"))
    pages = [
        hierarchy(
            _xml(**{"resource-id": "com.kakao.talk:id/message_edit_text", "bounds": "[0,1800][900,1900]"})
        ),
        hierarchy(_xml(text=PREVIEW, bounds="[10,400][800,560]")),
        hierarchy(_xml(text="답장", bounds="[100,500][300,580]")),
        hierarchy(
            _xml(**{"resource-id": "com.kakao.talk:id/message_edit_text", "bounds": "[0,1800][900,1900]"})
        ),
        hierarchy(
            _xml(**{"resource-id": "com.kakao.talk:id/send_button_layout", "bounds": "[1000,1800][1180,1900]"})
        ),
    ]
    calls, typed = [], []

    def adb(*args, check=True, timeout=25):
        calls.append(args)
        if args[:3] == ("shell", "dumpsys", "window"):
            return "mDreamingLockscreen=false"
        if args[:3] == ("shell", "dumpsys", "power"):
            return "Display Power: state=ON"
        if args[:3] == ("shell", "settings", "get"):
            return "com.android.inputmethod.latin/.LatinIME"
        if args[1] == "uiautomator":
            adb.page = pages.pop(0)
            return ""
        if args[1] == "cat":
            return adb.page
        if args[1] == "rm":
            adb.page = ""
            return ""
        if args[1] == "cmd":
            return "com.kakao.talk/com.kakao.talk.activity.main.AdaptiveMainActivity"
        return ""

    adb.page = ""
    driver = UiDriver(adb, lambda text: typed.append(text), sleep=lambda _seconds: None)
    driver.perform(
        {"kind": "reply", "chat_id": CHAT, "text": "답장 본문", "reply_preview": PREVIEW}
    )
    assert (
        "shell",
        "am",
        "start",
        "-n",
        ROOM_ACTIVITY,
        "--el",
        "key_id",
        CHAT,
        "--el",
        "chatId",
        CHAT,
        "--el",
        "chatRoomId",
        CHAT,
    ) in calls
    assert not any("kakaotalk://chatroom" in arg for call in calls for arg in call)
    assert any(call[:3] == ("shell", "input", "swipe") and call[-1] == "800" for call in calls)
    assert ("shell", "input", "tap", "200", "540") in calls
    assert typed == ["답장 본문"]
    assert calls[-2][:4] == ("shell", "am", "start", "-n")
    assert calls[-2][4].endswith("AdaptiveMainActivity")
    assert calls[-1] == ("shell", "ime", "set", "com.android.inputmethod.latin/.LatinIME",)
    assert any(call[1] == "rm" for call in calls)
    assert not list(tmp_path.glob("*.xml"))


def test_deeplink_is_only_the_fallback_and_failures_still_return_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KAKAO_UI_ACTIONS", "on")
    monkeypatch.setenv("KAKAO_SCREEN_LOCK", str(tmp_path / "screen.lock"))
    composer = hierarchy(
        _xml(**{"resource-id": "com.kakao.talk:id/send_button_layout", "bounds": "[1,1][4,4]"})
    )
    pages = [hierarchy(_xml(text="목록", bounds="[0,0][10,10]")), composer]
    calls = []
    clock = {"now": 0}

    def monotonic():
        clock["now"] += 1
        return clock["now"]

    def adb(*args, check=True, timeout=25):
        calls.append(args)
        if args[:3] == ("shell", "dumpsys", "window"):
            return "isKeyguardShowing=false"
        if args[:3] == ("shell", "dumpsys", "power"):
            return "Display Power: state=ON"
        if args[:3] == ("shell", "settings", "get"):
            return "not an ime id"
        if args[1] == "uiautomator":
            adb.page = pages.pop(0) if pages else composer
            return ""
        if args[1] == "cat":
            return adb.page
        if args[1] == "cmd":
            raise RuntimeError("resolve_failed")
        return ""

    adb.page = ""
    driver = UiDriver(
        adb, lambda _text: None, sleep=lambda _seconds: None, clock=monotonic, timeout=4
    )
    with pytest.raises(RuntimeError):
        driver.perform({"kind": "emoticon", "chat_id": CHAT, "emoticon_index": 0})
    deeplink = next(call for call in calls if "kakaotalk://chatroom/" + CHAT in call)
    assert deeplink[deeplink.index("-d") + 1] == "kakaotalk://chatroom/" + CHAT
    assert any(
        call[:6]
        == (
            "shell",
            "am",
            "start",
            "-a",
            "android.intent.action.MAIN",
            "-c",
        )
        for call in calls
    )
    assert any(call[1] == "rm" for call in calls)
    with pytest.raises(RuntimeError, match="ui_action_failed"):
        UiDriver(adb, lambda _text: None).perform({"kind": "reply", "chat_id": "1;rm", "text": "x"})
    assert not any("1;rm" in arg for call in calls for arg in call)


def test_ui_worker_fails_closed_when_the_flag_is_off(sending, monkeypatch):
    monkeypatch.delenv("KAKAO_UI_ACTIONS", raising=False)
    collector, client, _, body = sending
    monkeypatch.setenv("KAKAO_UI_ACTIONS", "on")
    enqueue(client, body | {"reply_to": "10"})
    monkeypatch.delenv("KAKAO_UI_ACTIONS", raising=False)
    monkeypatch.setattr(iris.cli, "adb", lambda *args, **kwargs: pytest.fail("no adb"))
    collector.send_pending(CONFIG)
    assert status(client, body | {"reply_to": "10"})["reason"] == "ui_actions_disabled"



def _scripted_adb(pages, calls):
    """Each uiautomator dump shows the next page; the last page repeats."""

    def adb(*args, check=True, timeout=25):
        calls.append(args)
        if args[:3] == ("shell", "dumpsys", "window"):
            return "mDreamingLockscreen=false"
        if args[:3] == ("shell", "dumpsys", "power"):
            return "Display Power: state=ON"
        if args[:3] == ("shell", "settings", "get"):
            return "com.android.inputmethod.latin/.LatinIME"
        if args[1] == "uiautomator":
            adb.page = pages.pop(0) if len(pages) > 1 else pages[0]
            return ""
        if args[1] == "cat":
            return adb.page
        if args[1] == "cmd":
            return "com.kakao.talk/com.kakao.talk.activity.main.AdaptiveMainActivity"
        return ""

    adb.page = ""
    return adb


EMO = "com.kakao.talk.emoticon:id/"
ROOM = _xml(**{"resource-id": "com.kakao.talk:id/message_edit_text", "bounds": "[684,1184][1046,1238]"})


def _emoticon_grid(*extra):
    return hierarchy(
        ROOM,
        _xml(**{"resource-id": EMO + "emoticon_grid", "bounds": "[597,1418][1187,1830]"}),
        _xml(**{"content-desc": "선택됨, 이모티콘, 탭, 4개 중 2번째", "bounds": "[787,1300][892,1347]"}),
        _xml(**{"content-desc": "최근사용 이모티콘 및 즐겨찾기", "bounds": "[606,1355][678,1418]"}),
        _xml(text="최근사용", **{"resource-id": EMO + "title", "bounds": "[597,1418][1187,1481]"}),
        _xml(**{"resource-id": EMO + "emoticon_icon", "bounds": "[623,1487][719,1583]"}),
        _xml(text="즐겨찾기", **{"resource-id": EMO + "title", "bounds": "[597,1589][1187,1652]"}),
        _xml(**{"resource-id": EMO + "emoticon_icon", "bounds": "[623,1658][719,1754]"}),
        _xml(**{"resource-id": EMO + "emoticon_icon", "bounds": "[770,1658][866,1754]"}),
        *extra,
    )


def test_emoticon_opens_closed_panel_and_taps_the_indexed_favorite(tmp_path, monkeypatch):
    monkeypatch.setenv("KAKAO_UI_ACTIONS", "on")
    monkeypatch.setenv("KAKAO_SCREEN_LOCK", str(tmp_path / "screen.lock"))
    closed = hierarchy(
        ROOM,
        _xml(**{"resource-id": "com.kakao.talk:id/emoticon_button", "bounds": "[1058,1193][1094,1229]"}),
    )
    preview = _emoticon_grid(
        _xml(**{"resource-id": EMO + "emoticon_preview_root", "bounds": "[751,953][1033,1163]"}),
        _xml(**{"resource-id": "com.kakao.talk:id/send_button_layout", "bounds": "[1106,1175][1172,1247]"}),
    )
    pages = [closed, closed, closed, _emoticon_grid(), _emoticon_grid(), _emoticon_grid(), preview, preview, closed]
    calls = []
    UiDriver(_scripted_adb(pages, calls), lambda _text: None, sleep=lambda _s: None).perform(
        {"kind": "emoticon", "chat_id": CHAT, "emoticon_index": 1}
    )
    taps = [call[3:] for call in calls if call[:3] == ("shell", "input", "tap")]
    # emoticon button, favorites tab, second favorite (not the recent one), send
    assert taps == [("1076", "1211"), ("642", "1386"), ("818", "1706"), ("1139", "1211")]
    assert not any("uiautomator" in call and "/sdcard" in " ".join(call) for call in calls)


def test_emoticon_does_not_toggle_an_open_panel_shut(tmp_path, monkeypatch):
    monkeypatch.setenv("KAKAO_UI_ACTIONS", "on")
    monkeypatch.setenv("KAKAO_SCREEN_LOCK", str(tmp_path / "screen.lock"))
    calls = []
    pages = [_emoticon_grid()]
    with pytest.raises(RuntimeError, match="ui_action_failed"):
        UiDriver(_scripted_adb(pages, calls), lambda _text: None, sleep=lambda _s: None).perform(
            {"kind": "emoticon", "chat_id": CHAT, "emoticon_index": 5}
        )
    taps = [call[3:] for call in calls if call[:3] == ("shell", "input", "tap")]
    assert taps == [("642", "1386")]


def test_location_searches_the_web_map_and_sends_the_first_place(tmp_path, monkeypatch):
    monkeypatch.setenv("KAKAO_UI_ACTIONS", "on")
    monkeypatch.setenv("KAKAO_SCREEN_LOCK", str(tmp_path / "screen.lock"))
    room = hierarchy(
        ROOM,
        _xml(**{"resource-id": "com.kakao.talk:id/media_send_layout", "content-desc": "미디어 전송 키보드 열기", "bounds": "[618,1175][678,1247]"}),
    )
    sheet = hierarchy(_xml(**{"content-desc": "지도", "bounds": "[0,1670][1200,1740]"}))
    dialog = hierarchy(
        _xml(text="위치서비스를 이용할 수 없습니다.", bounds="[351,829][849,955]"),
        _xml(text="취소", bounds="[675,964][768,1036]"),
    )
    blank = hierarchy(_xml(text="검색어를 입력하세요", bounds="[24,114][1176,183]"),
                      _xml(text="이 위치 보내기", bounds="[0,0][0,0]"))
    results = hierarchy(
        _xml(text="이 장소 보내기", bounds="[24,1107][1176,1162]"),
        _xml(text="이 장소 보내기", bounds="[24,1362][1176,1419]"),
    )
    pages = [room, room, sheet, dialog, blank, results, room]
    calls, typed = [], []
    UiDriver(_scripted_adb(pages, calls), typed.append, sleep=lambda _s: None).perform(
        {"kind": "location", "chat_id": CHAT, "location": {"title": "서울시청", "latitude": 37.5, "longitude": 127.0}}
    )
    taps = [call[3:] for call in calls if call[:3] == ("shell", "input", "tap")]
    assert taps == [("648", "1211"), ("600", "1705"), ("721", "1000"), ("600", "148"), ("600", "1134")]
    assert typed == ["서울시청"]
    assert ("shell", "input", "keyevent", "66") in calls


def test_step_log_has_names_and_timing_but_no_node_text(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KAKAO_UI_ACTIONS", "on")
    monkeypatch.setenv("KAKAO_SCREEN_LOCK", str(tmp_path / "screen.lock"))
    clock = {"now": 0.0}

    def monotonic():
        clock["now"] += 1
        return clock["now"]

    pages = [hierarchy(ROOM, _xml(text="비밀 메시지 본문", bounds="[0,0][10,10]"))]
    with pytest.raises(RuntimeError, match="ui_action_timeout"):
        UiDriver(_scripted_adb(pages, []), lambda _t: None, sleep=lambda _s: None, clock=monotonic, timeout=6).perform(
            {"kind": "location", "chat_id": CHAT, "location": {"title": "x"}}
        )
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert lines[-1]["ui_step"] == "location_media_button" and lines[-1]["ok"] is False
    assert all("elapsed_ms" in line for line in lines)
    assert "비밀" not in json.dumps(lines, ensure_ascii=False)


def test_activity_lookup_failure_is_a_known_fail_reason():
    from server.outgoing import FAIL_REASONS

    assert "kakao_activity_unavailable" in FAIL_REASONS
    assert "kakao_activity_unavailable" in iris._FAIL_REASONS


def test_aged_sweep_keeps_files_kakaotalk_may_still_read():
    removed = []
    mtimes = {f"{STAGE}/kb-0-old.mp4": "1000", f"{STAGE}/kb-0-new.mp4": "9000"}

    def adb(*args, check=True, timeout=25):
        if args[1] == "ls":
            return "kb-0-old.mp4\nkb-0-new.mp4\nKakaoTalk.jpg\n"
        if args[1] == "date":
            return "9100"
        if args[1] == "stat":
            return mtimes[args[-1]]
        removed.append(args)
        return ""

    sweep(adb, keep_seconds=1800)
    assert removed == [("shell", "rm", "-f", f"{STAGE}/kb-0-old.mp4")]


def test_failed_media_send_deletes_staged_files_now():
    from device.media_stage import release_after_send

    calls = []
    release_after_send(lambda *args, **kw: calls.append(args), ["kb-0-a.mp4"], False)
    assert calls == [("shell", "rm", "-f", f"{STAGE}/kb-0-a.mp4")]
    calls.clear()
    release_after_send(lambda *args, **kw: calls.append(args), ["kb-0-a.mp4"], True)
    assert calls == []
