#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

set -a
. ./agent.env
set +a

POLL_SECONDS="${POLL_SECONDS:-60}"
CONTROL_HOST="${CONTROL_HOST:-95.182.91.203}"
CONTROL_LOCAL_PORT="${CONTROL_LOCAL_PORT:-18765}"
DIRECT_BASELINE="${DIRECT_BASELINE:-1}"
EXTRA_INTERFACE_MAPS="${EXTRA_INTERFACE_MAPS:-}"
LOG_PATH="${LOG_PATH:-./managed-agent.log}"
export VPN_CONTROL_URL="http://127.0.0.1:${CONTROL_LOCAL_PORT}"

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*" | tee -a "$LOG_PATH"
}

ensure_tunnel() {
  if curl -fsS --connect-timeout 3 --max-time 5 "${VPN_CONTROL_URL}/healthz" >/dev/null 2>&1; then
    return 0
  fi
  log "starting SSH control tunnel on 127.0.0.1:${CONTROL_LOCAL_PORT}"
  nohup ./start_tunnel.sh >logs/control-tunnel.out.log 2>logs/control-tunnel.err.log &
  echo "$!" > run/control-tunnel.pid
  for _ in $(seq 1 15); do
    if curl -fsS --connect-timeout 3 --max-time 5 "${VPN_CONTROL_URL}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

build_interface_args() {
  local config_json="$1"
  python3 ./write_transport_plan.py "$config_json" --output-dir transports > run/transport-plan.json
  python3 - "$EXTRA_INTERFACE_MAPS" run/transport-plan.json <<'PY'
import json
import sys

extra = sys.argv[1].strip()
rows = json.load(open(sys.argv[2], encoding="utf-8"))
maps = []
for token in extra.replace(",", " ").split():
    if token:
        maps.append(token)
for row in rows:
    maps.append(f"{row['server_id']}={row['interface_name']}")
print("\n".join(maps))
PY
}

start_transports() {
  python3 - run/transport-plan.json <<'PY' | while IFS=$'\t' read -r name path; do
import json
import sys
for row in json.load(open(sys.argv[1], encoding="utf-8")):
    print(f"{row['interface_name']}\t{row['config_path']}")
PY
    [ -n "$name" ] || continue
    ./start_singbox_transport.sh "$name" "$path"
  done
}

stop_unused_transports() {
  local desired_file="$1"
  local pid_file name
  shopt -s nullglob
  for pid_file in run/*.pid; do
    name="$(basename "$pid_file" .pid)"
    [ "$name" = "control-tunnel" ] && continue
    if ! grep -Fxq "$name" "$desired_file"; then
      log "stopping unused sing-box transport: $name"
      ./stop_singbox_transport.sh "$name" || true
    fi
  done
  shopt -u nullglob
}

mkdir -p run logs transports
log "managed linux agent starting pid=$$ control=${VPN_CONTROL_URL}"

while true; do
  cycle_ok=0
  if ensure_tunnel; then
    if python3 ./route_agent.py config --json > run/fresh-config.json.tmp; then
      mv run/fresh-config.json.tmp run/fresh-config.json
      map_file="run/interface-maps.txt"
      build_interface_args run/fresh-config.json > "$map_file"
      start_transports
      python3 - run/transport-plan.json <<'PY' > run/desired-transports.txt
import json
import sys
for row in json.load(open(sys.argv[1], encoding="utf-8")):
    name = row.get("interface_name") or ""
    if name:
        print(name)
PY
      stop_unused_transports run/desired-transports.txt
      args=()
      while IFS= read -r map; do
        [ -n "$map" ] && args+=(--interface-map "$map")
      done < "$map_file"
      baseline_args=()
      [ "$DIRECT_BASELINE" = "1" ] && baseline_args+=(--direct-baseline)
      if [ "$(id -u)" -eq 0 ]; then
        python3 ./route_agent.py apply "${baseline_args[@]}" "${args[@]}" --yes --post-status
      else
        sudo env VPN_CONTROL_URL="$VPN_CONTROL_URL" VPN_AGENT_TOKEN="$VPN_AGENT_TOKEN" VPN_AGENT_DEVICE_ID="$VPN_AGENT_DEVICE_ID" \
          python3 ./route_agent.py apply "${baseline_args[@]}" "${args[@]}" --yes --post-status
      fi
      python3 ./route_agent.py probe-jobs "${args[@]}" --limit 2 || true
      log "cycle applied: maps=$(tr '\n' ',' < "$map_file" | sed 's/,$//')"
      cycle_ok=1
    else
      log "config fetch failed"
    fi
  else
    log "control tunnel failed"
  fi
  [ "${RUN_ONCE:-0}" = "1" ] && exit $((1 - cycle_ok))
  sleep "$POLL_SECONDS"
done
