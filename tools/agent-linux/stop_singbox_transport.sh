#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

name="${1:?usage: stop_singbox_transport.sh NAME}"
config_path="transports/${name}.json"
if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$0" "$name"
fi

pid_file="run/${name}.pid"
if [ -f "$pid_file" ]; then
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  cmd="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)"
  if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null && [[ "$cmd" == *sing-box*" run -c ${config_path}"* ]]; then
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
  fi
fi

if command -v pgrep >/dev/null 2>&1; then
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
  done < <(pgrep -f "sing-box run -c transports/${name}\\.json" 2>/dev/null || true)
fi

rm -f "$pid_file" "run/${name}.sha256"
if ip link show "$name" >/dev/null 2>&1; then
  ip link delete "$name" 2>/dev/null || true
fi
echo "stopped: $name"
