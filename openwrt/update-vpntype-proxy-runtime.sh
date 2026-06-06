#!/bin/sh
set -eu

AUTH_DEFAULT="${VPNTYPE_AUTH_DEFAULT:-}"
UUID_DEFAULT="${VPNTYPE_UUID_DEFAULT:-}"
API='https://vpntypedev.com/api/chrome/proxy-credentials'

[ -n "$AUTH_DEFAULT" ] && [ -n "$UUID_DEFAULT" ] || {
  echo "VPNTYPE_AUTH_DEFAULT and VPNTYPE_UUID_DEFAULT are required for generated refresh scripts." >&2
  exit 2
}

provider_country() {
  case "$1" in
    proxygb) echo GB ;;
    proxyca) echo CA ;;
    proxyfr) echo FR ;;
    proxyby) echo BY ;;
    proxyae) echo AE ;;
    proxyhk) echo HK ;;
    proxykz) echo KZ ;;
    proxytr) echo TR ;;
    proxyil) echo IL ;;
    proxycz) echo CZ ;;
    proxypl) echo PL ;;
    proxyfi) echo FI ;;
    proxynl) echo NL ;;
    proxyal) echo AL ;;
    proxyru) echo RU ;;
    proxyus) echo US ;;
    proxyde) echo DE ;;
    *) return 1 ;;
  esac
}

provider_candidate_ids() {
  case "$1" in
    proxygb) echo '142 85' ;;
    proxyca) echo '143 82' ;;
    proxyfr) echo '145 81' ;;
    proxyby) echo '146 80' ;;
    proxyae) echo '147 79' ;;
    proxyhk) echo '148 78' ;;
    proxykz) echo '149 77' ;;
    proxytr) echo '150 76' ;;
    proxyil) echo '151 75' ;;
    proxycz) echo '152 74' ;;
    proxypl) echo '153 61' ;;
    proxyfi) echo '154 60' ;;
    proxynl) echo '155 59' ;;
    proxyal) echo '156 58' ;;
    proxyru) echo '157 57' ;;
    proxyus) echo '158 56' ;;
    proxyde) echo '159 55' ;;
    *) return 1 ;;
  esac
}

write_refresh() {
  provider="$1"
  country="$2"
  candidate_ids="$3"
  refresh="/usr/bin/$provider-refresh"
  config="/etc/sing-box/$provider.json"

  cat > "$refresh" <<EOF_REFRESH
#!/bin/sh
set -eu

AUTH="\${VPNTYPE_AUTH:-$AUTH_DEFAULT}"
UUID="\${VPNTYPE_UUID:-$UUID_DEFAULT}"
COUNTRY='$country'
CANDIDATE_IDS='$candidate_ids'
CONFIG='$config'
API_CREDENTIALS='$API'
API_LIST='https://vpntypedev.com/api/chrome/proxy-list'
PROXY_CHECK_URL="\${PROXY_CHECK_URL:-https://ifconfig.me/ip}"

list_json="\${PROXY_LIST_JSON:-}"
if [ -z "\$list_json" ]; then
  list_json="\$(curl -fsS --connect-timeout 10 --max-time 25 -X POST "\$API_LIST" \\
    -H "Authorization: \$AUTH" \\
    -F "version=1.1.1" \\
    -F "uuid=\$UUID")"
fi

list_ids="\$(printf '%s\n' "\$list_json" \\
  | tr -d '\n' \\
  | sed 's/},{/}\\
{/g' \\
  | sed -n 's/.*"id":\([0-9][0-9]*\).*"country_id":"'$country'".*/\1/p' \\
  | tr '\n' ' ')"

candidate_ids="\$(printf '%s\n%s\n' "\$CANDIDATE_IDS" "\$list_ids" \\
  | tr ' ' '\n' \\
  | awk 'NF && !seen[\$1]++ { print \$1 }' \\
  | tr '\n' ' ')"

server=''
port=''
PROXY_ID=''

for candidate_id in \$candidate_ids; do
  json="\$(curl -fsS --connect-timeout 10 --max-time 25 -X POST "\$API_CREDENTIALS" \\
    -H "Authorization: \$AUTH" \\
    -F "version=1.1.1" \\
    -F "uuid=\$UUID" \\
    -F "proxy_id=\$candidate_id" 2>/dev/null || true)"

  credentials="\$(printf '%s\n' "\$json" | sed -n 's/.*"credentials":"\([^"]*\)".*/\1/p')"
  [ -n "\$credentials" ] || continue

  candidate_server="\${credentials%:*}"
  candidate_port="\${credentials##*:}"
  case "\$candidate_port" in
    *[!0-9]*|'') continue ;;
  esac

  if [ "\${VERIFY_PROXY:-1}" = "0" ] || curl -4 -fsS --connect-timeout 5 --max-time 12 \\
      -x "http://\$candidate_server:\$candidate_port" "\$PROXY_CHECK_URL" >/dev/null 2>&1; then
    server="\$candidate_server"
    port="\$candidate_port"
    PROXY_ID="\$candidate_id"
    break
  fi

  echo "$provider candidate failed: \$candidate_server:\$candidate_port id=\$candidate_id" >&2
done

[ -n "\$server" ] && [ -n "\$port" ] && [ -n "\$PROXY_ID" ] || {
  echo "No working proxy endpoint for $provider country \$COUNTRY; candidates: \$candidate_ids" >&2
  exit 1
}

old="\$(sed -n 's/.*"server": "\([^"]*\)".*/\1/p' "\$CONFIG" | head -1):\$(sed -n 's/.*"server_port": \([0-9][0-9]*\).*/\1/p' "\$CONFIG" | head -1)"
new="\$server:\$port"

service_running() {
  /etc/init.d/sing-box-$provider status 2>/dev/null | grep -q running
}

active_target() {
  grep -q "^TARGET_INTERFACE='$provider'" /usr/share/pbr/pbr.user.opencck-merged-vpn 2>/dev/null
}

if [ "\$old" != "\$new" ]; then
  cp "\$CONFIG" "\$CONFIG.bak-refresh-\$(date +%Y%m%d-%H%M%S)"
  sed -i \\
    -e 's/"server": "[^"]*"/"server": "'"\$server"'"/' \\
    -e 's/"server_port": [0-9][0-9]*/"server_port": '"\$port"'/' \\
    -e 's/"[0-9][0-9.]*\\/32"/"'"\$server"'\\/32"/' \\
    "\$CONFIG"
  sing-box check -c "\$CONFIG"
  if service_running || active_target; then
    /etc/init.d/sing-box-$provider restart
    sleep 2
  fi
  echo "Updated $provider proxy: \$old -> \$new id=\$PROXY_ID"
else
  sing-box check -c "\$CONFIG"
  if [ "\${START_ON_REFRESH:-0}" = "1" ] || active_target; then
    /etc/init.d/sing-box-$provider start || true
  fi
  echo "$provider proxy unchanged: \$new id=\$PROXY_ID"
fi
EOF_REFRESH
  chmod +x "$refresh"
}

write_switcher() {
  provider="$1"
  cat > "/usr/bin/vpn-$provider" <<EOF_SWITCH
#!/bin/sh
START_ON_REFRESH=1 /usr/bin/$provider-refresh || true
/etc/init.d/sing-box-$provider start || true
sleep 1
/usr/bin/vpn-switch $provider
sleep 1
ip route replace default dev $provider table pbr_$provider 2>/dev/null || true
EOF_SWITCH
  chmod +x "/usr/bin/vpn-$provider"
}

for provider in "$@"; do
  country="$(provider_country "$provider")" || {
    echo "Unknown provider: $provider" >&2
    exit 1
  }
  candidate_ids="$(provider_candidate_ids "$provider")"

  [ -f "/etc/sing-box/$provider.json" ] || {
    echo "Missing config: /etc/sing-box/$provider.json" >&2
    exit 1
  }
  [ -x "/etc/init.d/sing-box-$provider" ] || {
    echo "Missing service: /etc/init.d/sing-box-$provider" >&2
    exit 1
  }

  write_refresh "$provider" "$country" "$candidate_ids"
  write_switcher "$provider"
  "/usr/bin/$provider-refresh" || true
  "/etc/init.d/sing-box-$provider" enable
done

cat > /usr/bin/vpntype-proxy-refresh-all <<EOF_ALL
#!/bin/sh
set -eu

AUTH_DEFAULT='$AUTH_DEFAULT'
UUID_DEFAULT='$UUID_DEFAULT'
AUTH="\${VPNTYPE_AUTH:-\$AUTH_DEFAULT}"
UUID="\${VPNTYPE_UUID:-\$UUID_DEFAULT}"
API_LIST='https://vpntypedev.com/api/chrome/proxy-list'
PROVIDERS="\${*:-proxytr proxyhk proxyby proxykz proxyus proxyde proxynl proxyru proxygb proxyfi proxyal proxycz proxyae proxyca proxyil proxyfr proxypl}"
LOCK=/tmp/vpntype-proxy-refresh.lock

if ! mkdir "\$LOCK" 2>/dev/null; then
  echo "vpntype proxy refresh already running" >&2
  exit 0
fi
trap 'rmdir "\$LOCK"' EXIT

list_json="\$(curl -fsS --connect-timeout 10 --max-time 25 -X POST "\$API_LIST" \
  -H "Authorization: \$AUTH" \
  -F "version=1.1.1" \
  -F "uuid=\$UUID")"

rc=0
for provider in \$PROVIDERS; do
  if [ -x "/usr/bin/\$provider-refresh" ]; then
    PROXY_LIST_JSON="\$list_json" START_ON_REFRESH=0 "/usr/bin/\$provider-refresh" || rc=1
  fi
done

exit "\$rc"
EOF_ALL
chmod +x /usr/bin/vpntype-proxy-refresh-all

if [ -d /etc/crontabs ]; then
  touch /etc/crontabs/root
  grep -v 'vpntype-proxy-refresh-all' /etc/crontabs/root > /tmp/root.cron.$$
  echo '*/15 * * * * /usr/bin/vpntype-proxy-refresh-all >/tmp/vpntype-proxy-refresh-all.log 2>&1' >> /tmp/root.cron.$$
  cat /tmp/root.cron.$$ > /etc/crontabs/root
  rm -f /tmp/root.cron.$$
  /etc/init.d/cron enable >/dev/null 2>&1 || true
  /etc/init.d/cron restart >/dev/null 2>&1 || true
fi

if command -v crontab >/dev/null 2>&1; then
  crontab /etc/crontabs/root >/dev/null 2>&1 || true
fi

echo "Updated providers: $*"
