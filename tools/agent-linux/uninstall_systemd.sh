#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$0" "$@"
fi

[ -f ./agent.env ] && . ./agent.env
service_name="${1:-${SERVICE_NAME:-cudy-managed-agent.service}}"

if command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now "$service_name" || true
  rm -f "/etc/systemd/system/${service_name}"
  systemctl daemon-reload || true
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

./restore_direct.sh || true
echo "uninstalled: $service_name"
