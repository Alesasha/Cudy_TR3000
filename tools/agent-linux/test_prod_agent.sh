#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

set -a
. ./agent.env
set +a

CONTROL_LOCAL_PORT="${CONTROL_LOCAL_PORT:-18765}"
DOMAIN="${DOMAIN:-}"
EXPECTED_CIDR="${EXPECTED_CIDR:-}"
EXPECTED_ROUTE_IP="${EXPECTED_ROUTE_IP:-}"
EXPECTED_SERVER="${EXPECTED_SERVER:-}"
EXPECTED_INTERFACE="${EXPECTED_INTERFACE:-}"
PROBE_URL="${PROBE_URL:-https://ifconfig.me/ip}"
export VPN_CONTROL_URL="http://127.0.0.1:${CONTROL_LOCAL_PORT}"
mkdir -p run logs transports

control_health() {
  local attempt
  for attempt in 1 2 3 4 5 6; do
    if curl -fsS --connect-timeout 3 --max-time 5 "${VPN_CONTROL_URL}/healthz"; then
      echo
      return 0
    fi
    echo "control health attempt $attempt failed; retrying..." >&2
    sleep 2
  done
  echo "ERROR: control tunnel health check failed." >&2
  ./status.sh || true
  return 1
}

echo "== control tunnel =="
control_health

echo "== control config =="
test_config="$(mktemp "${TMPDIR:-/tmp}/cudy-agent-config.XXXXXX.json")"
selection_env="$(mktemp "${TMPDIR:-/tmp}/cudy-agent-selection.XXXXXX.env")"
trap 'rm -f "$test_config" "$selection_env"' EXIT
python3 ./route_agent.py config --json > "$test_config"
python3 - "$DOMAIN" "$EXPECTED_CIDR" "$EXPECTED_SERVER" "$EXPECTED_INTERFACE" "$EXPECTED_ROUTE_IP" "$test_config" > "$selection_env" <<'PY'
import json
import shlex
import sys

domain, expected_cidr, expected_server, expected_iface, expected_route_ip, path = sys.argv[1:7]
cfg = json.load(open(path, encoding="utf-8"))
route = None
route_label = ""
if domain:
    route = next((r for r in cfg.get("domain_routes", []) if r.get("domain") == domain), None)
    route_label = domain
if route is None and expected_cidr:
    route = next((r for r in cfg.get("ip_routes", []) if r.get("target_cidr") == expected_cidr), None)
    route_label = expected_cidr
if route is None:
    ip_routes = [r for r in cfg.get("ip_routes", []) if r.get("target_cidr") and r.get("server_id") not in ("", "direct", "auto")]
    if ip_routes:
        route = ip_routes[0]
        route_label = route.get("target_cidr") or ""
if route is None:
    domain_routes = [r for r in cfg.get("domain_routes", []) if r.get("domain") and r.get("server_id") not in ("", "direct", "auto")]
    if domain_routes:
        route = domain_routes[0]
        route_label = route.get("domain") or ""
        domain = route_label
if not route:
    raise SystemExit("no managed route in control config")
if not expected_server:
    expected_server = route.get("server_id") or ""
transport = next((t for t in cfg.get("transport_plan", []) if t.get("server_id") == expected_server), None)
if route.get("server_id") != expected_server:
    raise SystemExit(f"{route_label} resolved to {route.get('server_id')}, expected {expected_server}")
if not transport:
    raise SystemExit(f"no transport_plan for {expected_server}")
if not expected_iface:
    expected_iface = transport.get("interface_name") or expected_server
if transport.get("interface_name") != expected_iface:
    raise SystemExit(f"{expected_server} interface {transport.get('interface_name')}, expected {expected_iface}")
if not expected_cidr:
    expected_cidr = route.get("target_cidr") or ""
if not expected_route_ip and expected_cidr:
    expected_route_ip = expected_cidr.split("/", 1)[0]
if not expected_route_ip:
    raise SystemExit("no route IP selected for route verification")
print(f"DOMAIN={shlex.quote(domain)}")
print(f"EXPECTED_CIDR={shlex.quote(expected_cidr)}")
print(f"EXPECTED_ROUTE_IP={shlex.quote(expected_route_ip)}")
print(f"EXPECTED_SERVER={shlex.quote(expected_server)}")
print(f"EXPECTED_INTERFACE={shlex.quote(expected_iface)}")
print(f"ROUTE_LABEL={shlex.quote(route_label)}")
print(f"ROUTE_REQUESTED={shlex.quote(str(route.get('requested_server_id') or ''))}")
print(f"TRANSPORT_TYPE={shlex.quote(str(transport.get('transport_type') or ''))}")
PY
. "$selection_env"
echo "route=${ROUTE_LABEL} requested=${ROUTE_REQUESTED} resolved=${EXPECTED_SERVER}"
echo "transport=${EXPECTED_SERVER} type=${TRANSPORT_TYPE} iface=${EXPECTED_INTERFACE}"

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
