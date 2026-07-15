#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$0" "$@"
fi

[ -f ./agent.env ] && . ./agent.env
service_name="${1:-${SERVICE_NAME:-cudy-managed-agent.service}}"
service_base="${service_name%.service}"
watchdog_service="${service_base}-watchdog.service"
watchdog_timer="${service_base}-watchdog.timer"

if command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now "$watchdog_timer" 2>/dev/null || true
  systemctl stop "$watchdog_service" 2>/dev/null || true
  systemctl disable --now "$service_name" || true
  systemctl reset-failed "$service_name" 2>/dev/null || true
  rm -f "/etc/systemd/system/${service_name}"
  rm -f "/etc/systemd/system/${watchdog_service}" "/etc/systemd/system/${watchdog_timer}"
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

./restore_direct.sh || true
echo "uninstalled: $service_name"
