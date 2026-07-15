#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

[ -f ./agent.env ] && . ./agent.env
strip_cr() {
  printf '%s' "$1" | tr -d '\r'
}
service_name="$(strip_cr "${SERVICE_NAME:-cudy-managed-agent.service}")"

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

mkdir -p run logs transports
rm -f run/watchdog-tripped.json
chmod +x ./*.sh
if [ -f ./runtime/sing-box ]; then
  chmod +x ./runtime/sing-box || true
fi

if [ ! -f "/etc/systemd/system/${service_name}" ]; then
  ./install_systemd.sh "$service_name"
else
  systemctl daemon-reload
  systemctl enable "$service_name"
  systemctl restart "$service_name"
fi

sleep 2
systemctl --no-pager --full status "$service_name" || true
echo
echo "Cudy agent is ON."
