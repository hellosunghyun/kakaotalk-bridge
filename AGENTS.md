# AGENTS.md — setting up KakaoTalk Bridge

Instructions for AI coding agents (Codex, Claude Code, Cursor, etc.) that were handed this
repository URL and asked to "set it up". Follow the steps in order. Human-facing docs are in
Korean: [README](README.md) · [English README](README.en.md) · [Quickstart](docs/quickstart.md).

## What this is

A self-hosted MCP server that reads (and, with separate permission, sends) KakaoTalk
messages. It runs a virtual Android tablet (redroid) with the official KakaoTalk app, an
Iris collector that reads the on-device message DB, an API/MCP server, and a Caddy gateway,
all under Docker Compose. Releases ship prebuilt multi-arch images, so a host never needs to
compile Android code.

| Image (linux/amd64 + linux/arm64) | Services |
| --- | --- |
| `ghcr.io/hellosunghyun/kakaotalk-bridge/server:<version>` | `api`, `dot-plugin`, `mcp`, `dot-control`, `dot-tunnel` |
| `ghcr.io/hellosunghyun/kakaotalk-bridge/device:<version>` | `iris-collector`, `admin`, `device-agent` |
| `ghcr.io/hellosunghyun/kakaotalk-bridge/gateway:<version>` | `gateway`, `admin-local`, `dot-ingress` |

Releases: <https://github.com/hellosunghyun/kakaotalk-bridge/releases> (installer bundle,
`release.json` with image digests, signed keyboard APK, `SHA256SUMS`, build provenance).

## Safety rules (read first)

- **Never run `docker compose down -v`, `docker volume rm`, or delete the `android-data`
  volume.** It holds the KakaoTalk login and the collected DB. Stop with
  `kakaotalk-bridge stop`; remove everything only when the user explicitly asks
  (`kakaotalk-bridge cleanup`).
- **Never restart or recreate the `redroid` container to "fix" something** unless the user
  asks. Update other services with `--no-deps`.
- The KakaoTalk login, passkey creation, and phone confirmation are done **by the human** in
  the admin web UI. Do not type the user's KakaoTalk ID/password yourself unless they hand it
  to you for that purpose, and never store it in files, logs, or commits.
- Never commit `.env`, anything under `secrets/`, keystores, backups (`*.kcs`), or tokens.
- Sending messages is a separate permission. Only test sends in the user's own
  "나와의 채팅" (chat with myself).
- On login, KakaoTalk must offer **다른 기기와 함께 사용** (use together with another device).
  If it asks to migrate the account instead, stop and ask the user — continuing logs their
  phone out.

## 1. Check the host

Supported: a dedicated Linux host or VM (Ubuntu/Debian recommended; arm64 or x86_64) or an
Apple Silicon Mac (the installer creates a Lima VM). Start with 4 vCPU, 8 GB RAM, 20 GB free
disk. Unprivileged containers, most WSL setups, and locked-down corporate machines usually
fail because redroid needs the Android Binder kernel driver.

```bash
git clone https://github.com/hellosunghyun/kakaotalk-bridge.git
cd kakaotalk-bridge
bash scripts/preflight.sh          # read-only; exit 0 = ready, 1 = blocking problem
```

`preflight.sh` reports OS/arch, Docker Engine + Compose v2, Binder (`binder_linux` module or
binderfs), Python, memory/disk, and whether the default ports are free. On Ubuntu/Debian the
installer in step 2 installs Docker and loads Binder itself, so missing Docker/Binder there is
a warning, not a blocker. On other distros install them first:

```bash
# Binder for redroid (most Ubuntu/Debian kernels ship it as a module)
sudo modprobe binder_linux devices=binder,hwbinder,vndbinder
echo 'binder_linux' | sudo tee /etc/modules-load.d/binder.conf
echo 'options binder_linux devices=binder,hwbinder,vndbinder' | sudo tee /etc/modprobe.d/binder.conf
```

## 2. Install from the latest release (recommended)

Use the one-line installer. It downloads the verified release bundle, writes `.env` and
`secrets/`, pulls the prebuilt images (no local build), starts the services, and prints the
admin link. It needs `sudo` on Linux (install files are root-owned).

```bash
# Headless server / agent shell
curl -fsSL https://raw.githubusercontent.com/hellosunghyun/kakaotalk-bridge/main/install.sh \
  | bash -s -- --no-browser

# Pin a version instead of "latest"
curl -fsSL https://raw.githubusercontent.com/hellosunghyun/kakaotalk-bridge/main/install.sh \
  | BRIDGE_VERSION=v0.4.0 bash -s -- --no-browser
```

- Install folder: Linux `~/.local/share/kakaotalk-bridge` (override with `BRIDGE_HOME=...`),
  macOS `~/Library/Application Support/KakaoTalk Bridge`. The `kakaotalk-bridge` command is
  installed to `/usr/local/bin` (Linux) or `~/.local/bin` (macOS).
- Re-running the same command is safe: it resumes, or upgrades in place, keeping data.
- `--plan` shows the steps without changing anything; `--verbose` shows tool output;
  `--no-install` uses only already-installed host tools.
- Logs: `<install folder>/.bridge/logs/`.

### Alternative: install a verified release from this checkout

```bash
gh release download v0.4.0 -R hellosunghyun/kakaotalk-bridge -p release.json
gh attestation verify release.json --repo hellosunghyun/kakaotalk-bridge
./bridge install --manifest release.json   # pulls the digests pinned in release.json
./bridge passkey-login                      # prints the admin registration link
```

### Alternative: plain Docker Compose with release images (advanced)

Only if the user wants to manage Compose directly. `./bridge install` does all of this, plus
secret generation, so prefer it.

```bash
cp .env.example .env
cat >> .env <<'ENV'
SERVER_IMAGE=ghcr.io/hellosunghyun/kakaotalk-bridge/server:v0.4.0
DEVICE_IMAGE=ghcr.io/hellosunghyun/kakaotalk-bridge/device:v0.4.0
GATEWAY_IMAGE=ghcr.io/hellosunghyun/kakaotalk-bridge/gateway:v0.4.0
ENV
BRIDGE_PREBUILT=1 bash scripts/init-secrets.sh   # creates secrets/ (never commit it)
docker compose pull
docker compose up -d --no-build
```

### `.env` reference

`./bridge install` creates `.env` from [`.env.example`](.env.example); it contains no secrets
(those live in `secrets/`). Change values only for the reasons below.

| Variable | Default | When to change |
| --- | --- | --- |
| `COMPOSE_PROJECT_NAME` | `kakaotalk-bridge` | Never after first install (volume names depend on it). |
| `DEVICE_ID` | `personal-tablet` | Free-form label for the virtual tablet. |
| `REDROID_IMAGE` | pinned redroid 14 | Leave empty on arm64; `install` picks a 64-bit-only image. |
| `SERVER_IMAGE` / `DEVICE_IMAGE` / `GATEWAY_IMAGE` | set by the installer to release digests | Empty = build locally (`kakaotalk-bridge/*:local`). Use the same version for all three. |
| `DEVICE_SUBNET` / `DEVICE_IP_RANGE` | `172.29.87.0/24` / `172.29.87.128/25` | Before first install, if they overlap a LAN/VPN/Docker network. Range must lie inside the subnet. |
| `HTTPS_BIND` / `HTTPS_PORT` | `127.0.0.1` / `8443` | Collector ingress. Keep it on loopback. |
| `ADB_PORT` | `5555` | Host ADB port (loopback). Change if taken. |
| `RETENTION_DAYS` | `0` | `0` keeps messages forever; `N` keeps N days. |
| `DOT_PUBLIC_URL` | placeholder | Public HTTPS origin of the MCP endpoint, set via `kakaotalk-bridge connect` / `setup-connection`. |
| `DOT_HTTP_PORT` | `18787` | Loopback entry point for MCP/OAuth behind your HTTPS proxy. |
| `ADMIN_LOCAL_PORT` / `ADMIN_LOCAL_HOST` / `ADMIN_LOCAL_ORIGIN` | `18789` | Local admin UI; `up` sets these. |
| `OPENAI_TUNNEL_ENABLED` / `OPENAI_TUNNEL_ID` / `OPENAI_TUNNEL_IMAGE` | off | Optional ChatGPT tunnel (`kakaotalk-bridge tunnel configure`). |
| `DOT_APPROVAL_MODE` | `passkey` | MCP client approval method. |
| `ADMIN_AUTH_MODE` | `passkey` | Admin login method (`local` only as an explicit fallback). |
| `KAKAO_UI_ACTIONS` | empty (off) | `on` enables quote replies, emoticons and locations (UI automation). Must be set for both `api` and `iris-collector`. |
| `KAKAO_WEBHOOK_TARGETS` | empty | Prefer the file `secrets/webhooks/targets.json`; see [webhooks](docs/webhooks.md). |

## 3. First run: the human logs in

1. On a headless server, forward the admin port from the user's computer and keep it open:
   `ssh -N -L 127.0.0.1:18789:127.0.0.1:18789 user@your-server`
2. Get the link: `kakaotalk-bridge passkey-login` (or the link the installer printed). Open it
   **as `http://localhost:18789/...`** — passkeys do not work on LAN IPs.
3. Hand over to the user for these steps in the admin UI (see [web UI guide](docs/web-ui.md#first-login)):
   create a passkey → in the tablet's Aurora store choose **Anonymous** login and
   **Session Installer** → install **KakaoTalk by Kakao Corp.** → log in with
   **다른 기기와 함께 사용** → choose not to restore chats → confirm the phone is still logged
   in → **메시지 수집 시작**.
   (Offline alternative to Aurora: `kakaotalk-bridge import-apks /path/to/full-apk-set`.)
4. Ask the user to send a message to themselves from their phone and confirm it appears.

## 4. Connect an AI client (MCP)

```bash
kakaotalk-bridge setup-connection                          # interactive
kakaotalk-bridge setup-connection --method openai-tunnel   # ChatGPT via private tunnel
kakaotalk-bridge connect --url https://bridge.example.com  # existing HTTPS reverse proxy -> 127.0.0.1:18787
```

- Remote MCP endpoint: `https://<your-origin>/mcp` (OAuth + passkey approval). Admin stays at
  `/admin/`; block `/admin*` at the proxy if it must not be public.
- Local stdio MCP (AI app on the same machine or over SSH): command `kakaotalk-bridge mcp`.
  The SSH account must be in the `docker` group. See [API](docs/api.md#stdio-mcp).
- Tools: `get_recent_messages`, `search_messages`, `list_conversations`,
  `get_conversation_context`, `get_collector_status`, attachment reads, and — with the
  separate send permission — `send_message`, `send_media`, `send_emoticon`, `send_location`,
  `get_message_send_status`. See [sending](docs/sending.md).

## 5. Verify

```bash
kakaotalk-bridge doctor            # all checks should pass; --json for machine output
kakaotalk-bridge doctor --json | python3 -m json.tool
docker compose ps                  # run inside the install folder
```

Then ask the AI client: "카카오톡 수집 상태 알려줘" (`get_collector_status`). Day-2
operations, backups and recovery: [operations](docs/operations.md).

## Updating

```bash
kakaotalk-bridge upgrade                    # latest release; --version vX.Y.Z to pin
git pull && kakaotalk-bridge update --source  # git checkouts (builds locally)
```

Updates make an encrypted backup first and keep `.env`, `secrets/`, the Android data and the
KakaoTalk login. To swap a single service by hand: `docker compose pull <svc>` then
`docker compose up -d --no-deps --no-build <svc>` (never include `redroid`).

## Developing

```bash
uv sync --frozen --python 3.12
uv run pytest -q
uv run ruff check .
```

- `bash install.sh --source` / `./bridge install --source` builds all images locally. On
  arm64 the Android build runs under amd64 emulation and takes 1–3 hours; prefer CI images.
- Architecture and conventions: [development](docs/development.md), [design](docs/design.md).

<a id="releases"></a>
## Releases (maintainers)

Pushing a `vX.Y.Z` tag runs [`.github/workflows/release.yml`](.github/workflows/release.yml):
tests → signed APKs on x86_64 → native amd64/arm64 images → multi-arch tags
`:<version>` and `:latest` → GitHub Release with assets and provenance.
Pre-release tags (`vX.Y.Z-rc.1`) skip `:latest`.

Required repository secrets (APK signing; the key must stay the same across releases or
installed keyboards cannot be upgraded):

```bash
base64 -w0 secrets/bridge.jks | gh secret set BRIDGE_RELEASE_KEYSTORE_BASE64
gh secret set BRIDGE_RELEASE_KEY_PASSWORD < secrets/bridge_key_password
```

After the first release, set the three GHCR packages (server, device, gateway) to **Public**
(GitHub → Packages → package settings) so the installer can pull without logging in.
Branch pushes to `main` / `feat/**` also publish arm64 test images via
[`images.yml`](.github/workflows/images.yml) (tags: short SHA and branch name).
