#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

[ -f ./agent.env ] && . ./agent.env

strip_cr() {
  printf '%s' "$1" | tr -d '\r'
}

CONTROL_LOCAL_PORT="$(strip_cr "${CONTROL_LOCAL_PORT:-18765}")"
VPN_AGENT_TOKEN="$(strip_cr "${VPN_AGENT_TOKEN:-}")"
VPN_CONTROL_URL="http://127.0.0.1:${CONTROL_LOCAL_PORT}"

mkdir -p run logs

start_control_tunnel_if_needed() {
  if curl -fsS --connect-timeout 2 --max-time 4 "${VPN_CONTROL_URL}/healthz" >/dev/null 2>&1; then
    return 0
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
  : >logs/control-tunnel.out.log
  : >logs/control-tunnel.err.log
  nohup ./start_tunnel.sh >logs/control-tunnel.out.log 2>logs/control-tunnel.err.log &
  echo "$!" > run/control-tunnel.pid
  for _ in $(seq 1 20); do
    if curl -fsS --connect-timeout 2 --max-time 4 "${VPN_CONTROL_URL}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "ERROR: control tunnel is not ready." >&2
  tail -80 logs/control-tunnel.err.log >&2 || true
  exit 1
}

open_url() {
  local url="$1"
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 &
  else
    echo "$url"
  fi
}

start_control_tunnel_if_needed

if [ -n "$VPN_AGENT_TOKEN" ]; then
  open_url "${VPN_CONTROL_URL}/agent-login?token=${VPN_AGENT_TOKEN}"
else
  open_url "${VPN_CONTROL_URL}/"
fi
