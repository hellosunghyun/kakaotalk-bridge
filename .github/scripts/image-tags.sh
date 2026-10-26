#!/usr/bin/env bash
# Write image= and a tags list for docker/build-push-action.
# Usage: image-tags.sh <name> [extra-tag]
set -euo pipefail

name=${1:?image name}
extra=${2:-}
output=${GITHUB_OUTPUT:-/dev/stdout}
repo=$(printf '%s' "${GITHUB_REPOSITORY:?}" | tr '[:upper:]' '[:lower:]')
image="ghcr.io/${repo}/${name}"
sha=$(printf '%s' "${GITHUB_SHA:?}" | cut -c1-7)
safe=$(
  printf '%s' "${GITHUB_REF_NAME:-}" |
    tr '[:upper:]' '[:lower:]' |
    sed -E 's/[^a-z0-9._-]+/-/g; s/^[^a-z0-9_]+//; s/-+$//; s/-{2,}/-/g' |
    cut -c1-128
)

{
  printf 'image=%s\n' "$image"
  echo 'tags<<EOF'
  echo "${image}:${sha}"
  if [[ -n "$safe" && "$safe" != "$sha" ]]; then
    echo "${image}:${safe}"
  fi
  if [[ -n "$extra" ]]; then
    if [[ ! "$extra" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
      echo "refusing invalid image tag: $extra" >&2
      exit 1
    fi
    echo "${image}:${extra}"
  fi
  echo EOF
} >> "$output"
