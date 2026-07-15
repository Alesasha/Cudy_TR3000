#!/bin/sh
set -eu

PROVIDER="${1:-proxykz}"
PROXY_SERVER="${2:-}"
PROXY_PORT="${3:-}"
PROXY_TYPE="${PROXY_TYPE:-http}"
TUN_ADDRESS="${TUN_ADDRESS:-172.21.0.1/30}"
MTU="${MTU:-1400}"
PBR_SCRIPT="${PBR_SCRIPT:-/usr/share/pbr/pbr.user.opencck-merged-vpn}"
VPNTYPE_AUTH="${VPNTYPE_AUTH:-}"
VPNTYPE_UUID="${VPNTYPE_UUID:-}"
VPNTYPE_PROXY_ID="${VPNTYPE_PROXY_ID:-}"

case "$PROVIDER" in
  ''|*[!A-Za-z0-9_.-]*)
    echo "Invalid provider/interface name: $PROVIDER" >&2
    exit 2
    ;;
esac

[ -n "$PROXY_SERVER" ] && [ -n "$PROXY_PORT" ] || {
  echo "Usage: install-singbox-http-proxy.sh INTERFACE PROXY_HOST PROXY_PORT" >&2
  echo "Example: install-singbox-http-proxy.sh proxykz 185.175.46.23 49710" >&2
  exit 2
}

case "$PROXY_PORT" in
  *[!0-9]*|'')
    echo "Invalid proxy port: $PROXY_PORT" >&2
    exit 2
    ;;
esac

case "$PROXY_TYPE" in
  http|socks) ;;
  *)
    echo "Invalid PROXY_TYPE: $PROXY_TYPE; expected http or socks" >&2
    exit 2
    ;;
esac

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
[ -f "/etc/init.d/sing-box-$PROVIDER" ] && cp "/etc/init.d/sing-box-$PROVIDER" "$backup/" || true

cat > "/etc/sing-box/$PROVIDER.json" <<EOF
{
  "log": {
    "level": "info",
    "timestamp": true
  },
  "inbounds": [
    {
      "type": "tun",
      "tag": "$PROVIDER-tun",
      "interface_name": "$PROVIDER",
      "address": [
        "$TUN_ADDRESS"
      ],
      "mtu": $MTU,
      "auto_route": false,
      "strict_route": false,
      "stack": "gvisor"
    }
  ],
  "outbounds": [
    {
      "type": "$PROXY_TYPE",
      "tag": "proxy-out",
      "server": "$PROXY_SERVER",
      "server_port": $PROXY_PORT
    },
    {
      "type": "direct",
      "tag": "direct"
    },
    {
      "type": "block",
      "tag": "block"
    }
  ],
  "route": {
    "auto_detect_interface": true,
    "rules": [
      {
        "ip_cidr": [
          "$PROXY_SERVER/32"
        ],
        "outbound": "direct"
      }
    ],
    "final": "proxy-out"
  }
}
EOF
chmod 600 "/etc/sing-box/$PROVIDER.json"

if ! sing-box check -c "/etc/sing-box/$PROVIDER.json"; then
  echo "sing-box check failed for /etc/sing-box/$PROVIDER.json" >&2
  exit 1
fi

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

ensure_quic_reject() {
  src="$1"
  dest="$2"
  name="${src}_${dest}_quic_reject"
  if ! uci -q get "firewall.$name" >/dev/null 2>&1; then
    uci set "firewall.$name=rule"
  fi
  uci set "firewall.$name.name=Reject QUIC from $src to $dest"
  uci set "firewall.$name.src=$src"
  uci set "firewall.$name.dest=$dest"
  uci set "firewall.$name.proto=udp"
  uci set "firewall.$name.dest_port=443"
  uci set "firewall.$name.target=REJECT"
  uci set "firewall.$name.family=ipv4"
}

ensure_quic_reject lan "$PROVIDER"
if uci -q get firewall.friends >/dev/null 2>&1 || uci show firewall 2>/dev/null | grep -q "name='friends'"; then
  ensure_quic_reject friends "$PROVIDER"
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

if [ -n "$VPNTYPE_AUTH" ] && [ -n "$VPNTYPE_UUID" ] && [ -n "$VPNTYPE_PROXY_ID" ]; then
  cat > "/usr/bin/$PROVIDER-refresh" <<EOF
#!/bin/sh
set -eu

AUTH='$VPNTYPE_AUTH'
UUID='$VPNTYPE_UUID'
PROXY_ID='$VPNTYPE_PROXY_ID'
CONFIG='/etc/sing-box/$PROVIDER.json'
API='https://vpntypedev.com/api/chrome/proxy-credentials'

json="\$(curl -fsS --connect-timeout 10 --max-time 25 -X POST "\$API" \\
  -H "Authorization: \$AUTH" \\
  -F "version=1.1.1" \\
  -F "uuid=\$UUID" \\
  -F "proxy_id=\$PROXY_ID")"

credentials="\$(printf '%s\n' "\$json" | sed -n 's/.*"credentials":"\([^"]*\)".*/\1/p')"
[ -n "\$credentials" ] || {
  echo "Could not parse proxy credentials: \$json" >&2
  exit 1
}

server="\${credentials%:*}"
port="\${credentials##*:}"
case "\$port" in
  *[!0-9]*|'') echo "Invalid proxy port in credentials: \$credentials" >&2; exit 1 ;;
esac

old="\$(sed -n 's/.*"server": "\([^"]*\)".*/\1/p' "\$CONFIG" | head -1):\$(sed -n 's/.*"server_port": \([0-9][0-9]*\).*/\1/p' "\$CONFIG" | head -1)"
new="\$server:\$port"

if [ "\$old" != "\$new" ]; then
  cp "\$CONFIG" "\$CONFIG.bak-refresh-\$(date +%Y%m%d-%H%M%S)"
  sed -i \\
    -e '0,/"server": "[^"]*"/s//"server": "'"\$server"'"/' \\
    -e '0,/"server_port": [0-9][0-9]*/s//"server_port": '"\$port"'/' \\
    "\$CONFIG"
  sing-box check -c "\$CONFIG"
  /etc/init.d/sing-box-$PROVIDER restart
  sleep 2
  echo "Updated $PROVIDER proxy: \$old -> \$new"
else
  echo "$PROVIDER proxy unchanged: \$new"
fi
EOF
else
  cat > "/usr/bin/$PROVIDER-refresh" <<EOF
#!/bin/sh
echo "$PROVIDER refresh is not configured; using static $PROXY_TYPE proxy $PROXY_SERVER:$PROXY_PORT" >&2
exit 0
EOF
fi
chmod +x "/usr/bin/$PROVIDER-refresh"

cat > "/usr/bin/vpn-$PROVIDER" <<EOF
#!/bin/sh
/usr/bin/$PROVIDER-refresh || true
/etc/init.d/sing-box-$PROVIDER start || true
sleep 1
/usr/bin/vpn-switch $PROVIDER
sleep 1
ip route replace default dev $PROVIDER table pbr_$PROVIDER 2>/dev/null || true
EOF
chmod +x "/usr/bin/vpn-$PROVIDER"

"/etc/init.d/sing-box-$PROVIDER" enable
"/etc/init.d/sing-box-$PROVIDER" restart
sleep 2
/etc/init.d/network reload
/etc/init.d/firewall reload

if [ -x /usr/bin/cudy-pbr-safe-restart ]; then
  /usr/bin/cudy-pbr-safe-restart
elif [ -x /etc/init.d/pbr ]; then
  /etc/init.d/pbr restart
fi

echo "Installed sing-box $PROXY_TYPE proxy provider: $PROVIDER"
echo "Proxy: $PROXY_TYPE://$PROXY_SERVER:$PROXY_PORT"
echo "Config: /etc/sing-box/$PROVIDER.json"
echo "Backup: $backup"
echo "Switch command: vpn-$PROVIDER"
