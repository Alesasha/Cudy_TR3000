#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

REPO="${SING_BOX_REPO:-SagerNet/sing-box}"
VERSION="${SING_BOX_VERSION:-latest}"
RUNTIME_DIR="${SING_BOX_RUNTIME_DIR:-./runtime}"
FORCE="${FORCE:-0}"

if [ -x "${RUNTIME_DIR}/sing-box" ] && [ "$FORCE" != "1" ]; then
  echo "sing-box runtime already installed: ${RUNTIME_DIR}/sing-box"
  "${RUNTIME_DIR}/sing-box" version || true
  exit 0
fi

for cmd in python3 curl tar uname; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command is missing: $cmd" >&2
    exit 1
  fi
done

case "$(uname -m)" in
  x86_64|amd64) asset_arch="amd64" ;;
  aarch64|arm64) asset_arch="arm64" ;;
  armv7l|armv7) asset_arch="armv7" ;;
  *)
    echo "ERROR: unsupported CPU architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

asset_url="$(
  python3 - "$REPO" "$VERSION" "$asset_arch" <<'PY'
import json
import re
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

repo, version, arch = sys.argv[1:4]
api = f"https://api.github.com/repos/{repo}/releases/latest"
if version != "latest":
    api = f"https://api.github.com/repos/{repo}/releases/tags/{version}"
request = Request(api, headers={"User-Agent": "cudy-linux-agent-installer"})
try:
    with urlopen(request, timeout=30) as response:
        release = json.load(response)
except URLError as exc:
    raise SystemExit(f"ERROR: cannot query GitHub release API ({api}): {exc}") from exc
pattern = re.compile(rf"^sing-box-.*-linux-{re.escape(arch)}\.tar\.gz$")
matches = [
    asset.get("browser_download_url")
    for asset in release.get("assets", [])
    if pattern.match(asset.get("name", ""))
]
matches = [item for item in matches if item]
if not matches:
    names = ", ".join(asset.get("name", "") for asset in release.get("assets", []))
    raise SystemExit(f"no linux-{arch} tar.gz asset found in {release.get('tag_name')}: {names}")
print(matches[0])
PY
)"

archive="${tmp_dir}/sing-box.tar.gz"
echo "Downloading sing-box runtime: $asset_url"
curl -fL --connect-timeout 20 --max-time 300 "$asset_url" -o "$archive"
tar -xzf "$archive" -C "$tmp_dir"

bin_path="$(find "$tmp_dir" -type f -name sing-box -perm -u+x | head -1)"
if [ -z "${bin_path:-}" ]; then
  bin_path="$(find "$tmp_dir" -type f -name sing-box | head -1)"
fi
if [ -z "${bin_path:-}" ]; then
  echo "ERROR: downloaded archive does not contain sing-box" >&2
  exit 1
fi

mkdir -p "$RUNTIME_DIR"
cp "$bin_path" "${RUNTIME_DIR}/sing-box"
chmod 0755 "${RUNTIME_DIR}/sing-box"
echo "Installed sing-box runtime: ${RUNTIME_DIR}/sing-box"
"${RUNTIME_DIR}/sing-box" version || true
