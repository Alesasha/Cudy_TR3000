#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

set -a
. ./agent.env
set +a

CONTROL_LOCAL_PORT="${CONTROL_LOCAL_PORT:-18765}"
DOMAIN="${DOMAIN:-ifconfig.me}"
EXPECTED_SERVER="${EXPECTED_SERVER:-proxyde}"
EXPECTED_INTERFACE="${EXPECTED_INTERFACE:-proxyde}"
export VPN_CONTROL_URL="http://127.0.0.1:${CONTROL_LOCAL_PORT}"

echo "== control tunnel =="
curl -fsS --connect-timeout 5 --max-time 10 "${VPN_CONTROL_URL}/healthz"
echo

echo "== control config =="
python3 ./route_agent.py config --json > run/test-config.json
python3 - "$DOMAIN" "$EXPECTED_SERVER" "$EXPECTED_INTERFACE" run/test-config.json <<'PY'
import json
import sys

domain, expected_server, expected_iface, path = sys.argv[1:5]
cfg = json.load(open(path, encoding="utf-8"))
route = next((r for r in cfg.get("domain_routes", []) if r.get("domain") == domain), None)
transport = next((t for t in cfg.get("transport_plan", []) if t.get("server_id") == expected_server), None)
if not route:
    raise SystemExit(f"no domain route for {domain}")
if route.get("server_id") != expected_server:
    raise SystemExit(f"{domain} resolved to {route.get('server_id')}, expected {expected_server}")
if not transport:
    raise SystemExit(f"no transport_plan for {expected_server}")
if transport.get("interface_name") != expected_iface:
    raise SystemExit(f"{expected_server} interface {transport.get('interface_name')}, expected {expected_iface}")
print(f"domain={domain} requested={route.get('requested_server_id')} resolved={route.get('server_id')}")
print(f"transport={transport.get('server_id')} type={transport.get('transport_type')} iface={transport.get('interface_name')}")
PY

echo
echo "== route =="
ip route get "$(getent ahostsv4 "$DOMAIN" | awk 'NR==1 {print $1}')" || true

echo
echo "== pinned probe =="
curl -4 -sS --connect-timeout 10 --max-time 25 "https://${DOMAIN}/ip" || true
echo
echo "Linux agent smoke test completed."
