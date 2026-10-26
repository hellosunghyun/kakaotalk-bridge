#!/usr/bin/env bash
# Read-only host check before installing KakaoTalk Bridge. Changes nothing.
# Exit 0: ready (warnings may remain). Exit 1: a blocking problem was found.
# Usage: bash scripts/preflight.sh [--json]
set -uo pipefail

json=0
[[ "${1:-}" == "--json" ]] && json=1
fail=0
results=()

report() { # level name detail
  results+=("$1|$2|$3")
  [[ "$1" == FAIL ]] && fail=1
  return 0
}

os=$(uname -s)
arch=$(uname -m)
distro=""
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  distro=$(. /etc/os-release && printf '%s' "${ID:-}")
fi
debian_like=0
case "$distro" in ubuntu | debian) debian_like=1 ;; esac
# Missing tools are installed by the installer on Ubuntu/Debian; elsewhere they block.
missing_level=FAIL
[[ "$debian_like" == 1 ]] && missing_level=WARN

case "$os/$arch" in
  Linux/x86_64 | Linux/aarch64 | Linux/arm64) report OK platform "$os $arch ${distro:-unknown}" ;;
  Darwin/arm64) report OK platform "macOS arm64 (installer creates a Lima VM; Linux checks below are skipped)" ;;
  Darwin/*) report WARN platform "Intel Mac runs under QEMU Lima; unverified" ;;
  *) report FAIL platform "$os $arch is not supported (use a Linux arm64/x86_64 host)" ;;
esac

if grep -qi microsoft /proc/version 2>/dev/null; then
  report WARN wsl "WSL2 works only with Binder already loaded; not guaranteed"
fi

if [[ "$os" == Linux ]]; then
  if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1 || sudo -n docker info >/dev/null 2>&1; then
      report OK docker "$(docker --version 2>/dev/null)"
    else
      report WARN docker "docker is installed but the daemon is not reachable (start it or use sudo)"
    fi
    if docker compose version >/dev/null 2>&1; then
      report OK compose "$(docker compose version --short 2>/dev/null)"
    else
      report "$missing_level" compose "Docker Compose v2 plugin missing"
    fi
  else
    report "$missing_level" docker "Docker Engine not installed"
  fi

  if grep -qw binder /proc/filesystems 2>/dev/null || [[ -e /dev/binder ]] || [[ -d /dev/binderfs ]]; then
    report OK binder "Binder available"
  elif lsmod 2>/dev/null | grep -q '^binder_linux'; then
    report OK binder "binder_linux loaded"
  elif modinfo binder_linux >/dev/null 2>&1; then
    report WARN binder "binder_linux module exists but is not loaded (installer loads it on Ubuntu/Debian)"
  else
    report "$missing_level" binder "No Binder support in this kernel; redroid cannot start (try linux-modules-extra-\$(uname -r))"
  fi

  for tool in curl openssl; do
    if command -v "$tool" >/dev/null 2>&1; then report OK "$tool" "found"; else report FAIL "$tool" "missing"; fi
  done

  mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
  mem_gb=$((mem_kb / 1024 / 1024))
  if ((mem_gb >= 7)); then report OK memory "${mem_gb} GB"; else report WARN memory "${mem_gb} GB (8 GB recommended)"; fi

  if ((EUID != 0)) && ! sudo -n true 2>/dev/null; then
    report WARN sudo "installer needs sudo; it will prompt for a password"
  fi
fi

python_ok=0
for candidate in python3.14 python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
    "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' 2>/dev/null; then
    python_ok=1
    report OK python "$("$candidate" --version 2>&1)"
    break
  fi
done
[[ "$python_ok" == 0 ]] && report WARN python "Python 3.12+ not found (installer fetches one via uv)"

free_gb=$(df -Pk "${BRIDGE_HOME:-$HOME}" 2>/dev/null | awk 'NR==2 {print int($4/1024/1024)}')
if [[ -n "$free_gb" ]]; then
  if ((free_gb >= 20)); then report OK disk "${free_gb} GB free"; else report WARN disk "${free_gb} GB free (20 GB recommended)"; fi
fi

if command -v ss >/dev/null 2>&1; then
  for port in 18789 18787 8443 5555; do
    if ss -ltnH "sport = :$port" 2>/dev/null | grep -q .; then
      report WARN "port-$port" "already in use (fine if Bridge is already running; otherwise change it in .env)"
    fi
  done
fi

if [[ "$json" == 1 ]]; then
  printf '{"ready": %s, "checks": [' "$([[ $fail == 0 ]] && echo true || echo false)"
  sep=""
  for row in "${results[@]}"; do
    IFS='|' read -r level name detail <<<"$row"
    detail=${detail//\\/\\\\}
    detail=${detail//\"/\\\"}
    printf '%s{"level": "%s", "name": "%s", "detail": "%s"}' "$sep" "$level" "$name" "$detail"
    sep=", "
  done
  printf ']}\n'
else
  for row in "${results[@]}"; do
    IFS='|' read -r level name detail <<<"$row"
    printf '%-5s %-10s %s\n' "$level" "$name" "$detail"
  done
  if [[ "$fail" == 0 ]]; then
    echo "Ready. Next: see AGENTS.md step 2 (install)."
  else
    echo "Blocking problems found. Fix the FAIL lines, then run this again."
  fi
fi
exit "$fail"
