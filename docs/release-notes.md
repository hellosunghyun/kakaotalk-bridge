# 메시지·미디어 전송과 GitHub 릴리스

이 포크의 첫 GitHub Actions 릴리스입니다. AI가 기존 대화방으로 사진·동영상·파일을 보내고 받은 첨부를 읽을 수 있게 했고, 이미지를 amd64·arm64 러너에서 직접 만들어 GitHub Releases와 GHCR에 게시합니다.

## 주요 변경

- **미디어 전송:** `send_media`로 사진·동영상·음성·파일을 보냅니다. 카카오톡의 공유 화면을 그대로 사용합니다. 파일은 `application/octet-stream`으로 공유해 선택 창 없이 대화방으로 갑니다.
- **인용·이모티콘·위치 (선택):** `KAKAO_UI_ACTIONS=on`일 때만 동작하는 UI 자동화입니다. 기본값은 꺼짐입니다.
- **첨부 읽기:** `get_message_attachment`, `get_message_image`, `get_message_file`로 받은 사진·파일을 필요할 때 읽습니다. 서버에 바이트를 저장하지 않습니다.
- **웹훅 전달·즉시 응답:** 새 메시지를 정적으로 설정한 웹훅으로 전달하고, 설정한 방에서 키워드가 나오면 일반 전송 경로로 확인 메시지를 보낼 수 있습니다. [웹훅 안내](https://github.com/hellosunghyun/kakaotalk-bridge/blob/main/docs/webhooks.md)
- **보존 기간 무제한:** `RETENTION_DAYS=0`이면 메시지를 지우지 않습니다.
- **릴리스 이미지 위치 변경:** `ghcr.io/hellosunghyun/kakaotalk-bridge/{server,device,gateway}`(linux/amd64·linux/arm64). 버전 태그와 `latest`가 함께 붙습니다.
- **에이전트용 설치 안내:** 레포 링크만 받은 AI 코딩 에이전트가 따라 할 수 있는 [AGENTS.md](https://github.com/hellosunghyun/kakaotalk-bridge/blob/main/AGENTS.md)와 읽기 전용 점검 스크립트 `scripts/preflight.sh`를 추가했습니다.

## 설치·업데이트

```bash
curl -fsSL https://raw.githubusercontent.com/hellosunghyun/kakaotalk-bridge/main/install.sh | bash
```

화면 없는 서버는 `| bash -s -- --no-browser`를 쓰세요. 원래 rokrokss/kakaotalk-bridge 릴리스로 설치한 경우 이미지 저장소가 달라지므로 [삭제 안내](https://github.com/hellosunghyun/kakaotalk-bridge/blob/main/docs/operations.md#uninstall)대로 지운 뒤 새로 설치하는 것을 권장합니다.

## 릴리스 파일

| 파일 | 내용 |
| --- | --- |
| `bridge-install.tar.gz` | 설치 파일(소스와 `release.json`) |
| `release.json` | 서버·기기·게이트웨이 이미지 다이제스트 |
| `install.sh`, `install.ps1` | 설치 스크립트 |
| `kakaotalk-bridge-keyboard-<버전>.apk` | 릴리스 키로 서명한 Bridge 입력기 APK (이미지에도 포함) |
| `iris-collector-unsigned-<버전>.apk` | Iris 수집기 APK. 기기 안에서 설치할 때 서명합니다 (이미지에도 포함) |
| `SHA256SUMS` | 위 파일들의 체크섬 |

`gh attestation verify <파일> --repo hellosunghyun/kakaotalk-bridge`로 출처 증명을 확인할 수 있습니다.
