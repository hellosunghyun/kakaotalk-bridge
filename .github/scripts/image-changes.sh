#!/usr/bin/env bash
# Decide which images this workflow should build. Appends key=value to $GITHUB_OUTPUT.
set -euo pipefail

output=${GITHUB_OUTPUT:-/dev/stdout}
write() { printf '%s=%s\n' "$1" "$2" >> "$output"; }

event=${GITHUB_EVENT_NAME:-}
ref=${GITHUB_REF:-}
choice=${WORKFLOW_IMAGES:-all}
push=true
if [[ "$event" == "pull_request" ]]; then
  push=false
fi
write push "$push"

enable_all() {
  write device true
  write server true
  write gateway true
}

if [[ "$event" == "workflow_dispatch" ]]; then
  case "$choice" in
    all) enable_all ;;
    device)
      write device true
      write server false
      write gateway false
      ;;
    server)
      write device false
      write server true
      write gateway false
      ;;
    gateway)
      write device false
      write server false
      write gateway true
      ;;
    *)
      echo "unknown images input: $choice" >&2
      exit 1
      ;;
  esac
  exit 0
fi

if [[ "$ref" == refs/tags/* ]]; then
  enable_all
  exit 0
fi

base=${BASE_SHA:-}
if [[ -z "$base" || "$base" =~ ^0+$ ]]; then
  enable_all
  exit 0
fi

if ! git cat-file -e "${base}^{commit}" >/dev/null 2>&1; then
  enable_all
  exit 0
fi

changed=$(git diff --name-only --no-renames "$base" HEAD)
matches() { printf '%s\n' "$changed" | grep -Eq "$1"; }

# Device COPYs server/, webui/, and dot_plugin/ because the admin console runs from that image.
ci='\.github/workflows/images\.yml|\.github/scripts/image-changes\.sh|\.github/scripts/image-tags\.sh|\.github/scripts/flatten-apks\.sh'
device_re="^(docker/device\\.Dockerfile|docker/build-requirements\\.txt|requirements\\.lock|LICENSE|assets/logo\\.svg|${ci}|device/|server/|webui/|dot_plugin/|iris/|android/)"
server_re="^(docker/server\\.Dockerfile|docker/build-requirements\\.txt|requirements\\.lock|LICENSE|assets/logo\\.svg|${ci}|server/|dot_plugin/|ops/)"
gateway_re="^(docker/gateway\\.Dockerfile|${ci})"

device=false
server=false
gateway=false
if matches "$device_re"; then device=true; fi
if matches "$server_re"; then server=true; fi
if matches "$gateway_re"; then gateway=true; fi
write device "$device"
write server "$server"
write gateway "$gateway"
