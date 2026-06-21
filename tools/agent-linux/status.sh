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
ip -4 route show 0.0.0.0/1 2>/dev/null || true
ip -4 route show 128.0.0.0/1 2>/dev/null || true
ip route get 1.1.1.1 || true
CONTROL_HOST="${CONTROL_HOST:-95.182.91.203}"
ip route get "$CONTROL_HOST" || true

echo
echo "== dns and connectivity =="
if command -v getent >/dev/null 2>&1; then
  getent ahostsv4 ifconfig.me | head -3 || true
fi
if command -v resolvectl >/dev/null 2>&1; then
  resolvectl dns || true
  resolvectl domain || true
fi
if command -v dig >/dev/null 2>&1; then
  dig +time=3 +tries=1 ifconfig.me A | sed -n '1,18p' || true
  dig +time=3 +tries=1 @1.1.1.1 ifconfig.me A | sed -n '1,18p' || true
elif command -v nslookup >/dev/null 2>&1; then
  nslookup ifconfig.me || true
  nslookup ifconfig.me 1.1.1.1 || true
else
  echo "dig/nslookup is not available"
fi
if command -v ping >/dev/null 2>&1; then
  ping -c 2 -W 3 1.1.1.1 || true
fi
if command -v curl >/dev/null 2>&1; then
  curl -4 -sS --connect-timeout 5 --max-time 10 https://ifconfig.me/ip || true
  echo
fi

echo
echo "== vpn interfaces =="
ip -o link show | grep -Ei '(^[0-9]+: (amn|wg|awg|tun|ppp|sing|proxy|lokvpn)|wireguard|tun)' || true
ip -o -4 addr show | grep -Ei ' (amn|wg|awg|tun|ppp|sing|proxy|lokvpn)' || true

echo
echo "== vpn processes =="
if command -v pgrep >/dev/null 2>&1; then
  pgrep -af 'amnezia|wireguard|wg-quick|sing-box|openvpn|zapret|nfqws' || true
else
  ps aux | grep -Ei 'amnezia|wireguard|wg-quick|sing-box|openvpn|zapret|nfqws' | grep -v grep || true
fi

echo
echo "== firewall hints =="
if command -v ufw >/dev/null 2>&1; then
  ufw status verbose || true
else
  echo "ufw is not available"
fi
if command -v nft >/dev/null 2>&1; then
  nft list ruleset 2>/dev/null | grep -iE 'hook output|hook forward|policy drop| drop| reject|amnezia|wg|tun|zapret|nfqws' | head -120 || true
elif command -v iptables >/dev/null 2>&1; then
  iptables -S 2>/dev/null | grep -iE '^-P (OUTPUT|FORWARD) DROP|-j (DROP|REJECT)|amnezia|wg|tun|zapret|nfqws' | head -120 || true
else
  echo "nft/iptables is not available"
fi

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

echo
echo "== recent system hints =="
if command -v journalctl >/dev/null 2>&1; then
  journalctl -n 80 --no-pager 2>/dev/null | grep -iE 'cudy|amnezia|wireguard|sing-box|zapret|nfqws|drop|reject|denied|network unreachable|dns|resolved' | tail -40 || true
elif command -v dmesg >/dev/null 2>&1; then
  dmesg | grep -iE 'amnezia|wireguard|sing-box|zapret|nfqws|drop|reject|denied|network unreachable|dns' | tail -40 || true
fi
