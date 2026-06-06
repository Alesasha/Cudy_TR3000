#!/bin/sh
set -eu

PROVIDER="${1:-vpntype}"
CONFIG_SRC="${2:-/root/vpn-subscriptions/$PROVIDER.json}"
TUN_ADDRESS="${TUN_ADDRESS:-172.19.0.1/30}"
PBR_SCRIPT="${PBR_SCRIPT:-/usr/share/pbr/pbr.user.opencck-merged-vpn}"

case "$PROVIDER" in
  vpntype|lokvpn) ;;
  *)
    echo "Usage: install-singbox-provider.sh {vpntype|lokvpn} [config-json]" >&2
    exit 2
    ;;
esac

[ -s "$CONFIG_SRC" ] || {
  echo "Missing sing-box config: $CONFIG_SRC" >&2
  exit 1
}

command -v apk >/dev/null 2>&1 || {
  echo "apk package manager is required on this OpenWrt build" >&2
  exit 1
}

if ! command -v sing-box >/dev/null 2>&1; then
  apk update
  apk add sing-box
fi

mkdir -p /etc/sing-box /root/backup-singbox
backup="/root/backup-singbox/$PROVIDER-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup"

[ -f "/etc/sing-box/$PROVIDER.json" ] && cp "/etc/sing-box/$PROVIDER.json" "$backup/" || true
cp "$CONFIG_SRC" "/etc/sing-box/$PROVIDER.json"
chmod 600 "/etc/sing-box/$PROVIDER.json"

cat > "/etc/init.d/sing-box-$PROVIDER" <<EOF
#!/bin/sh /etc/rc.common
USE_PROCD=1
START=95
STOP=10

start_service() {
  procd_open_instance
  procd_set_param command /usr/bin/sing-box run -c /etc/sing-box/$PROVIDER.json
  procd_set_param respawn 3600 5 5
  procd_set_param stdout 1
  procd_set_param stderr 1
  procd_close_instance
}
EOF
chmod +x "/etc/init.d/sing-box-$PROVIDER"

if ! uci -q get "network.$PROVIDER" >/dev/null 2>&1; then
  uci set "network.$PROVIDER=interface"
fi
uci set "network.$PROVIDER.proto=none"
uci set "network.$PROVIDER.device=$PROVIDER"
uci commit network

if ! uci -q get "firewall.${PROVIDER}_zone" >/dev/null 2>&1; then
  uci set "firewall.${PROVIDER}_zone=zone"
fi
uci set "firewall.${PROVIDER}_zone.name=$PROVIDER"
uci set "firewall.${PROVIDER}_zone.network=$PROVIDER"
uci set "firewall.${PROVIDER}_zone.input=REJECT"
uci set "firewall.${PROVIDER}_zone.output=ACCEPT"
uci set "firewall.${PROVIDER}_zone.forward=REJECT"
uci set "firewall.${PROVIDER}_zone.masq=1"
uci set "firewall.${PROVIDER}_zone.mtu_fix=1"

ensure_forwarding() {
  src="$1"
  dest="$2"
  name="${src}_${dest}_forward"
  if ! uci -q get "firewall.$name" >/dev/null 2>&1; then
    uci set "firewall.$name=forwarding"
  fi
  uci set "firewall.$name.src=$src"
  uci set "firewall.$name.dest=$dest"
}

ensure_forwarding lan "$PROVIDER"
if uci -q get firewall.friends >/dev/null 2>&1 || uci show firewall 2>/dev/null | grep -q "name='friends'"; then
  ensure_forwarding friends "$PROVIDER"
fi
uci commit firewall

supported="$(uci -q get pbr.config.supported_interface 2>/dev/null || true)"
case " $supported " in
  *" $PROVIDER "*) ;;
  *)
    uci add_list pbr.config.supported_interface="$PROVIDER"
    uci commit pbr
    ;;
esac

"/etc/init.d/sing-box-$PROVIDER" enable
"/etc/init.d/sing-box-$PROVIDER" restart
sleep 2
/etc/init.d/network reload
/etc/init.d/firewall reload

if [ -x /etc/init.d/pbr ]; then
  /etc/init.d/pbr restart
fi

cat > "/usr/bin/vpn-$PROVIDER" <<EOF
#!/bin/sh
/etc/init.d/sing-box-$PROVIDER start || true
sleep 1
/usr/bin/vpn-switch $PROVIDER
sleep 1
ip route replace default dev $PROVIDER table pbr_$PROVIDER 2>/dev/null || true
EOF
chmod +x "/usr/bin/vpn-$PROVIDER"

echo "Installed sing-box provider: $PROVIDER"
echo "Config: /etc/sing-box/$PROVIDER.json"
echo "Backup: $backup"
echo "Switch command: vpn-$PROVIDER"
echo "Check:"
echo "  /etc/init.d/sing-box-$PROVIDER status"
echo "  ip -4 addr show dev $PROVIDER"
echo "  /root/check-pbr-switch.sh"
