#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

name="${1:?usage: start_singbox_transport.sh NAME CONFIG_PATH}"
config_path="${2:?usage: start_singbox_transport.sh NAME CONFIG_PATH}"

mkdir -p run logs

find_singbox() {
  if [ -x ./runtime/sing-box ]; then
    printf '%s\n' ./runtime/sing-box
    return 0
  fi
  if command -v sing-box >/dev/null 2>&1; then
    command -v sing-box
    return 0
  fi
  echo "ERROR: sing-box not found. Put it into ./runtime/sing-box or install it in PATH." >&2
  return 1
}

if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$0" "$name" "$config_path"
fi

bin="$(find_singbox)"
pid_file="run/${name}.pid"
hash_file="run/${name}.sha256"
new_hash="$(sha256sum "$config_path" | awk '{print $1}')"

running=0
if [ -f "$pid_file" ]; then
  old_pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [ -n "${old_pid:-}" ] && kill -0 "$old_pid" 2>/dev/null; then
    running=1
  fi
fi

old_hash=""
[ -f "$hash_file" ] && old_hash="$(cat "$hash_file" 2>/dev/null || true)"
if [ "$running" = "1" ] && [ "$old_hash" = "$new_hash" ]; then
  echo "sing-box transport already running: $name pid=$old_pid"
  exit 0
fi

if [ "$running" = "1" ]; then
  echo "restarting sing-box transport: $name pid=$old_pid"
  kill "$old_pid" 2>/dev/null || true
  sleep 1
  kill -9 "$old_pid" 2>/dev/null || true
fi

if ip link show "$name" >/dev/null 2>&1; then
  ip link delete "$name" 2>/dev/null || true
fi

nohup "$bin" run -c "$config_path" >"logs/${name}.out.log" 2>"logs/${name}.err.log" &
pid="$!"
printf '%s\n' "$pid" > "$pid_file"
printf '%s\n' "$new_hash" > "$hash_file"
sleep 2
if ! kill -0 "$pid" 2>/dev/null; then
  echo "ERROR: sing-box transport failed: $name" >&2
  tail -80 "logs/${name}.err.log" >&2 || true
  exit 1
fi
echo "sing-box transport running: $name pid=$pid config=$config_path"
