# 웹훅 전달 (정적 설정)

[MCP 이벤트](events.md) · [운영](operations.md)

새로 수집한 카카오톡 메시지를 원하는 서버로 HTTP POST로 보냅니다. [MCP 이벤트 구독](events.md)과 달리 OAuth·챌린지·24시간 TTL·관리 화면 대화 허용이 필요 없고, 서버 소유자가 설정 파일로 대상을 지정하며 **본문을 포함**해 보냅니다. 대상이 없으면 꺼져 있으며 기존 동작은 바뀌지 않습니다.

`dot-plugin` 컨테이너 안의 백그라운드 작업으로 실행됩니다(`dot_plugin/webhooks.py`). 수집기 API는 읽기 토큰으로만 읽고, 대상마다 커서와 전송 대기열을 `/data/webhooks.db`(dot-state 볼륨)에 저장합니다. 대기열에는 메시지 ID만 저장하며 본문은 전송할 때 다시 읽습니다.

## 설정

기본 설정 파일은 컨테이너의 `/run/webhooks/targets.json`이며 호스트의 `secrets/webhooks/targets.json`을 읽기 전용으로 연결합니다. `KAKAO_WEBHOOK_TARGETS` 환경 변수에 같은 JSON을 넣으면 파일보다 우선합니다(비밀값이 `docker inspect`에 보이므로 파일을 권장합니다).

```bash
# 서명 키 생성 (Standard Webhooks 형식)
python3 -c 'import base64,os;print("whsec_"+base64.b64encode(os.urandom(32)).decode())'

sudo install -d -m 0750 -o root -g 10001 secrets/webhooks
sudo install -m 0640 -o root -g 10001 /dev/stdin secrets/webhooks/targets.json <<'JSON'
{
  "targets": [
    {
      "id": "my-server",
      "url": "https://hooks.example.com/kakao",
      "secret": "whsec_...",
      "conversation_refs": ["personal-tablet:...:474912824730238"],
      "mentions": ["@김성현"],
      "include_mine": false
    }
  ]
}
JSON
sudo docker compose --profile dot up -d --no-deps --force-recreate dot-plugin
```

| 필드 | 필수 | 설명 |
| --- | --- | --- |
| `id` | 예 | 영문·숫자·`_`·`-` 1–64자. 커서와 대기열의 키이므로 바꾸면 새 대상으로 취급합니다. |
| `url` | 예 | 기본은 공개 HTTPS만 허용합니다. |
| `secret` / `secret_file` | 예 | `whsec_` + base64(24–64바이트). `secret_file`은 컨테이너 안 경로입니다. |
| `conversation_refs` | 아니요 | `list_conversations`의 정확한 대화 참조 목록. 비우면 모든 대화. |
| `mentions` | 아니요 | 본문에 이 문자열 중 하나가 있을 때만 전송(대소문자 무시 부분 일치). 비우면 모든 메시지. |
| `include_mine` | 아니요 | 내가 보낸 메시지도 전송. 기본 `false`. |
| `allow_tailnet` | 아니요 | Tailscale 주소(`100.64.0.0/10`, `fd7a:115c:a1e0::/48`)를 허용하고, 이 주소로만 `http://`를 허용. 기본 `false`. |
| `enabled` | 아니요 | `false`이면 무시. |

설정이 잘못되면 MCP 서버는 그대로 실행되고 웹훅만 꺼지며 로그에 `{"component":"webhooks","state":"disabled_invalid_config","reason":...}`가 남습니다. 정상이면 `"state":"enabled"`가 남습니다. 컨테이너 안의 `localhost`는 컨테이너 자신이므로 호스트의 서버로 보내려면 호스트의 Tailscale 주소와 `allow_tailnet`을 사용하세요.

## 페이로드

```json
{
  "type": "message.created",
  "id": "msg_3f0c…",
  "target_id": "my-server",
  "cursor_epoch": "b0914358-…",
  "message_id": "5778",
  "sent_at": "2026-10-25T16:07:19Z",
  "collected_at": "2026-10-25T16:07:21.123456+00:00",
  "conversation": {"ref": "personal-tablet:…:474912824730238", "name": "AI", "kind": "MultiChat"},
  "sender": {"ref": "personal-tablet:…:58271094", "name": "혜림"},
  "is_mine": false,
  "body": "@김성현 이거 봐줘",
  "message_type": "1",
  "attachment_type": "text",
  "has_attachment": false,
  "truncated": false,
  "mentions": {"keywords": ["@김성현"], "matched": true}
}
```

`attachment_type`과 `has_attachment`는 종류 코드 힌트입니다. 사진이면 `photo`와 `true`이며 URL은 넣지 않습니다. 본문을 보려면 기존처럼 페이로드의 `body`를, 이미지를 보려면 MCP `get_message_image`를 사용합니다.

헤더: `webhook-id`(= `id`), `webhook-timestamp`, `webhook-signature: v1,<base64>`, `X-Kakao-Webhook-Target`. 서명은 [Standard Webhooks](https://www.standardwebhooks.com/) 방식으로 `id.timestamp.body`의 HMAC-SHA256입니다.

```python
import base64, hashlib, hmac, time

def verify(secret, headers, body: bytes, tolerance=300):
    msg_id, ts = headers["webhook-id"], headers["webhook-timestamp"]
    if abs(time.time() - int(ts)) > tolerance:
        raise ValueError("stale")
    key = base64.b64decode(secret.removeprefix("whsec_"))
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    if not any(hmac.compare_digest(expected, s.split(",", 1)[1]) for s in headers["webhook-signature"].split()):
        raise ValueError("bad signature")
```

## 전달 규칙

- 대상을 처음 켜거나 DB 복구로 커서 세대가 바뀌면 **현재 수집 커서부터** 시작하며 이전 메시지는 보내지 않습니다.
- 커서와 대기열이 저장되므로 재시작해도 처음부터 다시 보내거나 메시지를 잃지 않습니다. 적어도 한 번 전달이므로 같은 `id`가 다시 올 수 있습니다. 수신 서버는 `id`로 중복을 제거하세요.
- 2xx는 성공입니다. 400·410·413·422와 3xx(리디렉션을 따르지 않음)는 실패로 끝냅니다. 그 밖의 응답·시간 초과·연결 실패는 10초부터 두 배씩(최대 1시간) 늘려 최대 10회 재시도합니다.
- 실패한 대상은 수집이나 다른 대상, MCP 조회를 막지 않습니다. 대상별 대기열이 1000건을 넘으면 그 대상의 커서만 멈춥니다.
- 순서는 보장하지 않습니다. `message_id`와 `sent_at`으로 정렬하세요.
- DNS로 확인한 주소에 직접 연결하고 HTTPS는 원래 호스트 이름으로 인증서를 검증합니다. 사설·루프백·링크 로컬 주소는 항상 거부합니다.
- 로그에는 대상 `id`, 상태 코드, 시도 횟수만 남기며 URL·비밀값·본문은 남기지 않습니다.
- 본문은 신뢰할 수 없는 데이터입니다. 수신 서버에서 본문 내용을 지시로 실행하지 마세요.

테스트: `tests/test_webhooks.py`.
