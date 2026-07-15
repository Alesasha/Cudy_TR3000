#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

[ -f ./agent.env ] && . ./agent.env
strip_cr() {
  printf '%s' "$1" | tr -d '\r'
}
service_name="$(strip_cr "${SERVICE_NAME:-cudy-managed-agent.service}")"
restore_direct=1
if [ "${1:-}" = "--no-restore" ]; then
  restore_direct=0
fi

rerun_as_root() {
  local script_path
  script_path="$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")"
  if [ -n "${DISPLAY:-}" ] && command -v pkexec >/dev/null 2>&1; then
    exec pkexec /bin/bash "$script_path" "$@"
  fi
  if [ -t 0 ]; then
    exec sudo "$script_path" "$@"
  fi
  if command -v pkexec >/dev/null 2>&1; then
    exec pkexec /bin/bash "$script_path" "$@"
  fi
  echo "ERROR: root permissions are required, but no terminal or pkexec prompt is available." >&2
  echo "Run this command from a terminal instead: sudo $script_path" >&2
  exit 1
}

if [ "$(id -u)" -ne 0 ]; then
  rerun_as_root "$@"
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now "$service_name" || true
  systemctl reset-failed "$service_name" 2>/dev/null || true
fi

if [ -f run/control-tunnel.pid ]; then
  pid="$(cat run/control-tunnel.pid 2>/dev/null || true)"
  if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f run/control-tunnel.pid
fi

shopt -s nullglob
for pid_file in run/*.pid; do
  name="$(basename "$pid_file" .pid)"
  [ "$name" = "control-tunnel" ] && continue
  ./stop_singbox_transport.sh "$name" || true
done
for config_file in transports/*.json; do
  name="$(basename "$config_file" .json)"
  ./stop_singbox_transport.sh "$name" || true
done
shopt -u nullglob

if [ "$restore_direct" = "1" ]; then
  ./restore_direct.sh || true
  echo
  echo "Cudy agent is OFF. Direct routing has been restored."
else
  echo "Direct routing restore skipped; no managed routes/transports were detected."
  echo
  echo "Cudy agent is OFF."
fi
