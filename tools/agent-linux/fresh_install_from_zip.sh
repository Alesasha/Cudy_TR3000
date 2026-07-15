#!/usr/bin/env bash
set -euo pipefail

work_dir="$(pwd)"
archive_arg="${1:-}"

find_archive() {
  local candidate
  if [ -n "$archive_arg" ]; then
    printf '%s\n' "$archive_arg"
    return 0
  fi
  for candidate in ./*linux-prod.zip ./*.zip; do
    if [ -f "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

archive="$(find_archive || true)"
if [ -z "${archive:-}" ]; then
  echo "ERROR: pass agent zip path, or put *linux-prod.zip into this directory." >&2
  echo "Usage: bash ./fresh_install_from_zip.sh DC_via_Cudy-linux-prod.zip" >&2
  exit 1
fi

if [ ! -f "$archive" ]; then
  echo "ERROR: archive not found: $archive" >&2
  exit 1
fi

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/cudy-agent-install.XXXXXX")"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

cp -f "$archive" "$tmp_dir/package.zip"

echo "== stop previous service if present =="
if command -v systemctl >/dev/null 2>&1; then
  sudo systemctl disable --now cudy-managed-agent.service 2>/dev/null || true
  sudo systemctl reset-failed cudy-managed-agent.service 2>/dev/null || true
fi

echo "== remove subdirectories in $work_dir =="
find "$work_dir" -mindepth 1 -maxdepth 1 -type d -print0 | while IFS= read -r -d '' dir; do
  echo "remove: $dir"
  sudo chmod -R u+rwX "$dir" 2>/dev/null || true
  sudo rm -rf --one-file-system "$dir"
done

echo "== unpack fresh package =="
if command -v unzip >/dev/null 2>&1; then
  unzip -o "$tmp_dir/package.zip" -d "$work_dir"
else
  python3 - "$tmp_dir/package.zip" "$work_dir" <<'PY'
import sys
import zipfile
archive, target = sys.argv[1:3]
with zipfile.ZipFile(archive) as zf:
    zf.extractall(target)
PY
fi

echo "== make scripts executable =="
chmod +x "$work_dir"/*.sh
if [ -f "$work_dir/runtime/sing-box" ]; then
  chmod +x "$work_dir/runtime/sing-box"
fi

echo "== install =="
cd "$work_dir"
sudo ./one_click_install.sh

echo
echo "== production smoke test =="
./test_prod_agent.sh
