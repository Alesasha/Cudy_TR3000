#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

set -a
[ -f ./agent.env ] && . ./agent.env
[ -f ./run/control-endpoint.env ] && . ./run/control-endpoint.env
set +a

strip_cr() {
  printf '%s' "$1" | tr -d '\r'
}

CONTROL_HOST="$(strip_cr "${CONTROL_HOST:-95.182.91.203}")"
CONTROL_HOST_KEY_SHA256="$(strip_cr "${CONTROL_HOST_KEY_SHA256:-}")"
CONTROL_PORT="$(strip_cr "${CONTROL_PORT:-22}")"
CONTROL_USER="$(strip_cr "${CONTROL_USER:-cudy-tunnel-linux}")"
CONTROL_LOCAL_PORT="$(strip_cr "${CONTROL_LOCAL_PORT:-18765}")"
CONTROL_REMOTE_PORT="$(strip_cr "${CONTROL_REMOTE_PORT:-8765}")"
SSH_KEY="$(strip_cr "${SSH_KEY:-./uswest_control_tunnel_ed25519}")"
CONTROL_CONNECT_TIMEOUT="$(strip_cr "${CONTROL_CONNECT_TIMEOUT:-12}")"
KNOWN_HOSTS_FILE="$(strip_cr "${KNOWN_HOSTS_FILE:-./known_hosts}")"
STRICT_HOST_KEY_CHECKING=accept-new

prepare_known_host() {
  local lookup temp fingerprint
  [ -n "$CONTROL_HOST_KEY_SHA256" ] || {
    [ -f "$KNOWN_HOSTS_FILE" ] && STRICT_HOST_KEY_CHECKING=yes
    return 0
  }
  command -v ssh-keygen >/dev/null 2>&1 || {
    echo "ssh-keygen is required to verify the advertised control-server key" >&2
    return 1
  }
  command -v ssh-keyscan >/dev/null 2>&1 || {
    echo "ssh-keyscan is required to verify the advertised control-server key" >&2
    return 1
  }
  mkdir -p run "$(dirname "$KNOWN_HOSTS_FILE")"
  lookup="$CONTROL_HOST"
  [ "$CONTROL_PORT" = "22" ] || lookup="[$CONTROL_HOST]:$CONTROL_PORT"
  temp="run/control-host-key.$$"
  rm -f "$temp"
  if ssh-keygen -F "$lookup" -f "$KNOWN_HOSTS_FILE" 2>/dev/null \
      | awk '!/^#/ && NF {print}' > "$temp" \
      && [ -s "$temp" ] \
      && ssh-keygen -lf "$temp" -E sha256 2>/dev/null \
        | awk '{print $2}' | grep -Fxq "$CONTROL_HOST_KEY_SHA256"; then
    rm -f "$temp"
    STRICT_HOST_KEY_CHECKING=yes
    return 0
  fi
  rm -f "$temp"
  ssh-keyscan -T "$CONTROL_CONNECT_TIMEOUT" -p "$CONTROL_PORT" -t ed25519 "$CONTROL_HOST" > "$temp" 2>/dev/null || true
  fingerprint="$(ssh-keygen -lf "$temp" -E sha256 2>/dev/null | awk 'NR == 1 {print $2}')"
  if [ "$fingerprint" != "$CONTROL_HOST_KEY_SHA256" ]; then
    echo "control-server SSH key mismatch: expected $CONTROL_HOST_KEY_SHA256, got ${fingerprint:-none}" >&2
    rm -f "$temp"
    return 1
  fi
  touch "$KNOWN_HOSTS_FILE"
  chmod 600 "$KNOWN_HOSTS_FILE"
  ssh-keygen -R "$lookup" -f "$KNOWN_HOSTS_FILE" >/dev/null 2>&1 || true
  cat "$temp" >> "$KNOWN_HOSTS_FILE"
  rm -f "$temp"
  STRICT_HOST_KEY_CHECKING=yes
}

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
prepare_known_host
chmod 600 "$SSH_KEY"
echo "control route:"
ip route get "$CONTROL_HOST" || true
exec ssh \
  -i "$SSH_KEY" \
  -p "$CONTROL_PORT" \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o PasswordAuthentication=no \
  -o KbdInteractiveAuthentication=no \
  -o StrictHostKeyChecking="$STRICT_HOST_KEY_CHECKING" \
  -o UserKnownHostsFile="$KNOWN_HOSTS_FILE" \
  -o ExitOnForwardFailure=yes \
  -o ConnectTimeout="$CONTROL_CONNECT_TIMEOUT" \
  -o ConnectionAttempts=1 \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o LogLevel=VERBOSE \
  -N -L "${CONTROL_LOCAL_PORT}:127.0.0.1:${CONTROL_REMOTE_PORT}" \
  "${CONTROL_USER}@${CONTROL_HOST}"
