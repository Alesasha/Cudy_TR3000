#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

set -a
. ./agent.env
set +a

CONTROL_LOCAL_PORT="${CONTROL_LOCAL_PORT:-18765}"
DOMAIN="${DOMAIN:-}"
EXPECTED_CIDR="${EXPECTED_CIDR:-149.154.160.0/20}"
EXPECTED_ROUTE_IP="${EXPECTED_ROUTE_IP:-149.154.160.1}"
EXPECTED_SERVER="${EXPECTED_SERVER:-proxyde}"
EXPECTED_INTERFACE="${EXPECTED_INTERFACE:-proxyde}"
PROBE_URL="${PROBE_URL:-https://ifconfig.me/ip}"
export VPN_CONTROL_URL="http://127.0.0.1:${CONTROL_LOCAL_PORT}"

echo "== control tunnel =="
curl -fsS --connect-timeout 5 --max-time 10 "${VPN_CONTROL_URL}/healthz"
echo

echo "== control config =="
python3 ./route_agent.py config --json > run/test-config.json
python3 - "$DOMAIN" "$EXPECTED_CIDR" "$EXPECTED_SERVER" "$EXPECTED_INTERFACE" run/test-config.json <<'PY'
import json
import sys

domain, expected_cidr, expected_server, expected_iface, path = sys.argv[1:6]
cfg = json.load(open(path, encoding="utf-8"))
route = None
route_label = ""
if domain:
    route = next((r for r in cfg.get("domain_routes", []) if r.get("domain") == domain), None)
    route_label = domain
if route is None and expected_cidr:
    route = next((r for r in cfg.get("ip_routes", []) if r.get("target_cidr") == expected_cidr), None)
    route_label = expected_cidr
transport = next((t for t in cfg.get("transport_plan", []) if t.get("server_id") == expected_server), None)
if not route:
    raise SystemExit(f"no managed route for {domain or expected_cidr}")
if route.get("server_id") != expected_server:
    raise SystemExit(f"{route_label} resolved to {route.get('server_id')}, expected {expected_server}")
if not transport:
    raise SystemExit(f"no transport_plan for {expected_server}")
if transport.get("interface_name") != expected_iface:
    raise SystemExit(f"{expected_server} interface {transport.get('interface_name')}, expected {expected_iface}")
print(f"route={route_label} requested={route.get('requested_server_id')} resolved={route.get('server_id')}")
print(f"transport={transport.get('server_id')} type={transport.get('transport_type')} iface={transport.get('interface_name')}")
PY

echo
echo "== route =="
route_out="$(ip route get "$EXPECTED_ROUTE_IP" || true)"
printf '%s\n' "$route_out"
if ! printf '%s\n' "$route_out" | grep -q "dev $EXPECTED_INTERFACE"; then
  echo "ERROR: $EXPECTED_ROUTE_IP is not routed via $EXPECTED_INTERFACE" >&2
  exit 1
fi

echo
echo "== connectivity probe =="
curl -4 -sS --connect-timeout 10 --max-time 25 "$PROBE_URL" || true
echo
echo "Linux agent smoke test completed."
