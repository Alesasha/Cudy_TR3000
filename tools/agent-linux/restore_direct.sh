#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$0" "$@"
fi

keep_transports=0
if [ "${1:-}" = "--keep-transports" ]; then
  keep_transports=1
fi
force_half_routes=0
if [ "${RESTORE_FORCE_HALF_ROUTES:-0}" = "1" ]; then
  force_half_routes=1
fi

default_line="$(ip -4 route show default | awk '
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

gw="$(printf '%s' "$default_line" | awk '{print $1}')"
dev="$(printf '%s' "$default_line" | awk '{print $2}')"

if [ -z "${dev:-}" ]; then
  echo "ERROR: could not find a non-VPN default route." >&2
  ip -4 route show >&2 || true
  exit 1
fi

cleanup_half_route() {
  local prefix="$1"
  while ip -4 route show "$prefix" 2>/dev/null | grep -q .; do
    ip route del "$prefix" 2>/dev/null || break
  done
}

replace_direct_half() {
  local prefix="$1"
  if [ "$force_half_routes" = "1" ]; then
    if [ -n "${gw:-}" ]; then
      ip route replace "$prefix" via "$gw" dev "$dev"
    else
      ip route replace "$prefix" dev "$dev"
    fi
    return
  fi
  cleanup_half_route "$prefix"
}

replace_direct_half "0.0.0.0/1"
replace_direct_half "128.0.0.0/1"

shopt -s nullglob
if [ "$keep_transports" != "1" ]; then
  for pid_file in run/*.pid; do
    name="$(basename "$pid_file" .pid)"
    [ "$name" = "control-tunnel" ] && continue
    ./stop_singbox_transport.sh "$name" || true
  done
fi
shopt -u nullglob

if command -v resolvectl >/dev/null 2>&1; then
  ip -o link show | awk -F': ' '{print $2}' | cut -d'@' -f1 | while IFS= read -r link_name; do
    case "$link_name" in
      amn*|wg*|awg*|tun*|ppp*|sing*|proxy*|lokvpn*)
        [ "$link_name" = "$dev" ] && continue
        resolvectl revert "$link_name" >/dev/null 2>&1 || true
        resolvectl default-route "$link_name" no >/dev/null 2>&1 || true
        ;;
    esac
  done
  dns_value="${RESTORE_DNS_SERVERS:-${gw:-192.168.1.1} 1.1.1.1}"
  read -r -a dns_servers <<< "$dns_value"
  if [ "${#dns_servers[@]}" -gt 0 ]; then
    resolvectl dns "$dev" "${dns_servers[@]}" || true
  fi
  resolvectl domain "$dev" "${RESTORE_DNS_DOMAIN:-~.}" || true
  resolvectl default-route "$dev" yes || true
fi

echo "Direct baseline restored via ${gw:-on-link} dev $dev"
ip route get 1.1.1.1 || true
