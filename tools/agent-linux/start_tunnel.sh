#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

set -a
[ -f ./agent.env ] && . ./agent.env
set +a

CONTROL_HOST="${CONTROL_HOST:-95.182.91.203}"
CONTROL_PORT="${CONTROL_PORT:-22}"
CONTROL_USER="${CONTROL_USER:-cudy-tunnel-linux}"
CONTROL_LOCAL_PORT="${CONTROL_LOCAL_PORT:-18765}"
CONTROL_REMOTE_PORT="${CONTROL_REMOTE_PORT:-8765}"
SSH_KEY="${SSH_KEY:-./uswest_control_tunnel_ed25519}"

pin_control_route() {
  local gw_dev gw dev
  gw_dev="$(ip -4 route show default | awk '
    $1 == "default" {
      gw="";
      dev="";
      for (i=1; i<=NF; i++) {
        if ($i == "via") gw=$(i+1);
        if ($i == "dev") dev=$(i+1);
      }
      if (dev !~ /^(amn|wg|awg|tun|ppp|sing|proxy|lokvpn)/) {
        print gw, dev;
        exit;
      }
    }
  ')"
  gw="$(printf '%s' "$gw_dev" | awk '{print $1}')"
  dev="$(printf '%s' "$gw_dev" | awk '{print $2}')"
  [ -n "${dev:-}" ] || return 0
  if [ "$(id -u)" -eq 0 ]; then
    if [ -n "${gw:-}" ]; then
      ip route replace "${CONTROL_HOST}/32" via "$gw" dev "$dev"
    else
      ip route replace "${CONTROL_HOST}/32" dev "$dev"
    fi
  else
    if [ -n "${gw:-}" ]; then
      sudo ip route replace "${CONTROL_HOST}/32" via "$gw" dev "$dev"
    else
      sudo ip route replace "${CONTROL_HOST}/32" dev "$dev"
    fi
  fi
}

pin_control_route
chmod 600 "$SSH_KEY"
echo "control route:"
ip route get "$CONTROL_HOST" || true
exec ssh \
  -i "$SSH_KEY" \
  -p "$CONTROL_PORT" \
  -o ExitOnForwardFailure=yes \
  -o ConnectTimeout=20 \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -N -L "${CONTROL_LOCAL_PORT}:127.0.0.1:${CONTROL_REMOTE_PORT}" \
  "${CONTROL_USER}@${CONTROL_HOST}"
