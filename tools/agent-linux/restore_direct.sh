#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$0" "$@"
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

replace_direct_half() {
  local prefix="$1"
  if [ -n "${gw:-}" ]; then
    ip route replace "$prefix" via "$gw" dev "$dev"
  else
    ip route replace "$prefix" dev "$dev"
  fi
}

replace_direct_half "0.0.0.0/1"
replace_direct_half "128.0.0.0/1"

shopt -s nullglob
for pid_file in run/*.pid; do
  name="$(basename "$pid_file" .pid)"
  [ "$name" = "control-tunnel" ] && continue
  ./stop_singbox_transport.sh "$name" || true
done
shopt -u nullglob

if command -v resolvectl >/dev/null 2>&1; then
  resolvectl dns "$dev" "${RESTORE_DNS_SERVERS:-192.168.1.254 1.1.1.1}" || true
  resolvectl domain "$dev" "${RESTORE_DNS_DOMAIN:-~.}" || true
fi

echo "Direct baseline restored via ${gw:-on-link} dev $dev"
ip route get 1.1.1.1 || true
