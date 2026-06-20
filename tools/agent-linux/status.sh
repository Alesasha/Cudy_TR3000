#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

[ -f ./agent.env ] && . ./agent.env

SERVICE_NAME="${SERVICE_NAME:-cudy-managed-agent.service}"
CONTROL_LOCAL_PORT="${CONTROL_LOCAL_PORT:-18765}"
VPN_CONTROL_URL="${VPN_CONTROL_URL:-http://127.0.0.1:${CONTROL_LOCAL_PORT}}"

echo "== service =="
if command -v systemctl >/dev/null 2>&1; then
  systemctl --no-pager --full status "$SERVICE_NAME" || true
else
  echo "systemctl is not available"
fi

echo
echo "== control =="
if command -v curl >/dev/null 2>&1; then
  curl -fsS --connect-timeout 3 --max-time 5 "${VPN_CONTROL_URL}/healthz" || true
  echo
else
  echo "curl is not available"
fi

echo
echo "== routes =="
ip -4 route show default || true
ip route get 1.1.1.1 || true
CONTROL_HOST="${CONTROL_HOST:-95.182.91.203}"
ip route get "$CONTROL_HOST" || true

echo
echo "== managed transports =="
shopt -s nullglob
found=0
for pid_file in run/*.pid; do
  found=1
  name="$(basename "$pid_file" .pid)"
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
    echo "$name pid=$pid running"
  else
    echo "$name pid=${pid:-?} stopped"
  fi
done
shopt -u nullglob
[ "$found" = "1" ] || echo "no managed pid files"

echo
echo "== log tail =="
tail -60 "${LOG_PATH:-./managed-agent.log}" 2>/dev/null || true
