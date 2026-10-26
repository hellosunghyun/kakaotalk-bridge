#!/usr/bin/env bash
# Ensure APK build outputs sit at the root of the artifact directory.
# buildx local exports sometimes nest a single platform under linux_amd64/.
set -euo pipefail

dest=${1:?apk artifact directory}
mkdir -p "$dest"
if [[ ! -f "$dest/iris.apk" ]]; then
  found=$(find "$dest" -name iris.apk -print -quit)
  if [[ -z "$found" ]]; then
    echo "iris.apk not found under $dest" >&2
    find "$dest" -maxdepth 3 -print >&2 || true
    exit 1
  fi
  parent=$(dirname "$found")
  find "$parent" -maxdepth 1 -type f -exec mv -f {} "$dest/" \;
fi
for name in apksigner.jar bridge.apk iris.apk iris-source.tar.gz iris-dependencies.txt; do
  if [[ ! -s "$dest/$name" ]]; then
    echo "missing $dest/$name" >&2
    exit 1
  fi
done
