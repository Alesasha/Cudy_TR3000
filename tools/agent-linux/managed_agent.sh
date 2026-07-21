#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

set -a
. ./agent.env
set +a

strip_cr() {
  printf '%s' "$1" | tr -d '\r'
}

POLL_SECONDS="$(strip_cr "${POLL_SECONDS:-60}")"
CONTROL_HOST="$(strip_cr "${CONTROL_HOST:-95.182.91.203}")"
CONTROL_HOST_KEY_SHA256="$(strip_cr "${CONTROL_HOST_KEY_SHA256:-}")"
CONTROL_USER="$(strip_cr "${CONTROL_USER:-cudy-tunnel-linux}")"
CONTROL_LOCAL_PORT="$(strip_cr "${CONTROL_LOCAL_PORT:-18765}")"
CONTROL_REMOTE_PORT="$(strip_cr "${CONTROL_REMOTE_PORT:-8765}")"
CONTROL_TUNNEL_WAIT_SECONDS="$(strip_cr "${CONTROL_TUNNEL_WAIT_SECONDS:-25}")"
DIRECT_BASELINE="$(strip_cr "${DIRECT_BASELINE:-1}")"
EXTRA_INTERFACE_MAPS="$(strip_cr "${EXTRA_INTERFACE_MAPS:-}")"
LOG_PATH="$(strip_cr "${LOG_PATH:-./managed-agent.log}")"
VPN_AGENT_TOKEN="$(strip_cr "${VPN_AGENT_TOKEN:-}")"
VPN_AGENT_DEVICE_ID="$(strip_cr "${VPN_AGENT_DEVICE_ID:-}")"
AGENT_AUTO_UPDATE="$(strip_cr "${AGENT_AUTO_UPDATE:-1}")"
export VPN_AGENT_TOKEN VPN_AGENT_DEVICE_ID
export VPN_CONTROL_URL="http://127.0.0.1:${CONTROL_LOCAL_PORT}"

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*" | tee -a "$LOG_PATH"
}

load_cached_control_endpoint() {
  if [ -f run/control-endpoint.env ]; then
    # Generated locally from an authenticated /api/agent/config response.
    . run/control-endpoint.env
    CONTROL_HOST="$(strip_cr "${CONTROL_HOST:-}")"
    CONTROL_HOST_KEY_SHA256="$(strip_cr "${CONTROL_HOST_KEY_SHA256:-}")"
    export CONTROL_HOST CONTROL_HOST_KEY_SHA256
  fi
}

cache_authenticated_control_endpoint() {
  local config_file="$1"
  python3 - "$config_file" run/control-endpoint.env.tmp <<'PY'
import json
import re
import shlex
import sys

config_path, output_path = sys.argv[1:]
root = json.load(open(config_path, encoding="utf-8"))
manifest = ((root.get("control") or {}).get("endpoints") or {})
selected = None
for endpoint in manifest.get("endpoints") or []:
    if not isinstance(endpoint, dict) or str(endpoint.get("role") or "").lower() != "primary":
        continue
    tunnel = endpoint.get("ssh_tunnel") or {}
    host = str(tunnel.get("host") or "").strip()
    fingerprint = str(tunnel.get("host_key_sha256") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", host):
        continue
    if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{20,}={0,2}", fingerprint):
        continue
    priority = int(endpoint.get("priority") or 1000)
    candidate = (priority, host, fingerprint)
    if selected is None or candidate[0] < selected[0]:
        selected = candidate
if selected is None:
    raise SystemExit(0)
_, host, fingerprint = selected
with open(output_path, "w", encoding="ascii", newline="\n") as handle:
    handle.write("CONTROL_HOST=" + shlex.quote(host) + "\n")
    handle.write("CONTROL_HOST_KEY_SHA256=" + shlex.quote(fingerprint) + "\n")
PY
  if [ -s run/control-endpoint.env.tmp ]; then
    chmod 600 run/control-endpoint.env.tmp
    mv run/control-endpoint.env.tmp run/control-endpoint.env
  else
    rm -f run/control-endpoint.env.tmp
  fi
}

stop_control_tunnel() {
  local pid
  if [ -f run/control-tunnel.pid ]; then
    pid="$(cat run/control-tunnel.pid 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
      log "stopping stale SSH control tunnel pid=$pid"
      kill "$pid" 2>/dev/null || true
      sleep 1
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f run/control-tunnel.pid
  fi
}

cleanup_orphaned_control_tunnels() {
  local proc pid cmd tracked_pid=""
  [ -f run/control-tunnel.pid ] && tracked_pid="$(cat run/control-tunnel.pid 2>/dev/null || true)"
  for proc in /proc/[0-9]*/cmdline; do
    pid="${proc#/proc/}"
    pid="${pid%/cmdline}"
    [ "$pid" = "$tracked_pid" ] && continue
    cmd="$(cat "$proc" 2>/dev/null | tr '\0' ' ' || true)"
    case "$cmd" in
      *"-L ${CONTROL_LOCAL_PORT}:127.0.0.1:${CONTROL_REMOTE_PORT}"*"${CONTROL_USER}@${CONTROL_HOST}"*)
        log "stopping orphaned SSH control tunnel pid=$pid"
        kill "$pid" 2>/dev/null || true
        sleep 1
        kill -9 "$pid" 2>/dev/null || true
        ;;
    esac
  done
}

dump_control_tunnel_logs() {
  log "control tunnel stderr tail:"
  tail -80 logs/control-tunnel.err.log 2>/dev/null | tee -a "$LOG_PATH" || true
  log "control tunnel stdout tail:"
  tail -40 logs/control-tunnel.out.log 2>/dev/null | tee -a "$LOG_PATH" || true
}

ensure_tunnel() {
  if curl -fsS --connect-timeout 3 --max-time 5 "${VPN_CONTROL_URL}/healthz" >/dev/null 2>&1; then
    return 0
  fi
  stop_control_tunnel
  cleanup_orphaned_control_tunnels
  : >logs/control-tunnel.out.log
  : >logs/control-tunnel.err.log
  log "starting SSH control tunnel on 127.0.0.1:${CONTROL_LOCAL_PORT}"
  nohup ./start_tunnel.sh >logs/control-tunnel.out.log 2>logs/control-tunnel.err.log &
  echo "$!" > run/control-tunnel.pid
  for _ in $(seq 1 "$CONTROL_TUNNEL_WAIT_SECONDS"); do
    if curl -fsS --connect-timeout 3 --max-time 5 "${VPN_CONTROL_URL}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  log "control tunnel did not become ready within ${CONTROL_TUNNEL_WAIT_SECONDS}s"
  dump_control_tunnel_logs
  stop_control_tunnel
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

apply_config() {
  local config_file="$1"
  local control_online="$2"
  local map_file="run/interface-maps.txt"
  build_interface_args "$config_file" > "$map_file"
  start_transports
  restore_direct_dns_baseline
  python3 - run/transport-plan.json <<'PY' > run/desired-transports.txt
import json
import sys
for row in json.load(open(sys.argv[1], encoding="utf-8")):
    name = row.get("interface_name") or ""
    if name:
        print(name)
PY
  args=()
  while IFS= read -r map; do
    [ -n "$map" ] && args+=(--interface-map "$map")
  done < "$map_file"
  baseline_args=()
  [ "$DIRECT_BASELINE" = "1" ] && baseline_args+=(--direct-baseline)
  cached_args=()
  status_args=()
  if [ "$control_online" = "0" ]; then
    cached_args+=(--cached)
  else
    status_args+=(--post-status)
  fi
  if [ "$(id -u)" -eq 0 ]; then
    python3 ./route_agent.py apply "${cached_args[@]}" "${baseline_args[@]}" "${args[@]}" --yes --can-manage-transports "${status_args[@]}"
  else
    sudo env VPN_CONTROL_URL="$VPN_CONTROL_URL" VPN_AGENT_TOKEN="$VPN_AGENT_TOKEN" VPN_AGENT_DEVICE_ID="$VPN_AGENT_DEVICE_ID" \
      python3 ./route_agent.py apply "${cached_args[@]}" "${baseline_args[@]}" "${args[@]}" --yes --can-manage-transports "${status_args[@]}"
  fi
  if [ "$control_online" = "1" ]; then
    python3 ./route_agent.py probe-jobs "${args[@]}" --limit 2 || true
  else
    log "probe jobs skipped because control is unavailable"
  fi
  stop_unused_transports run/desired-transports.txt
  log "cycle applied: maps=$(tr '\n' ',' < "$map_file" | sed 's/,$//')"
}

restore_direct_dns_baseline() {
  [ -x ./restore_direct.sh ] || return 0

  local default_line gw dev identity previous="" prefix route_line stable=1
  default_line="$(ip -4 route show default | awk '
    $1 == "default" {
      gw=""; dev="";
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
  [ -n "${dev:-}" ] || stable=0
  identity="${gw:-on-link}|${dev:-missing}|${DIRECT_BASELINE}"
  [ -f run/direct-baseline.identity ] && previous="$(cat run/direct-baseline.identity 2>/dev/null || true)"
  [ "$identity" = "$previous" ] || stable=0

  for prefix in 0.0.0.0/1 128.0.0.0/1; do
    route_line="$(ip -4 route show "$prefix" 2>/dev/null || true)"
    if [ "$DIRECT_BASELINE" = "1" ]; then
      [ "$(printf '%s\n' "$route_line" | grep -c . || true)" = "1" ] || stable=0
      printf '%s\n' "$route_line" | grep -Eq "(^| )dev ${dev}($| )" || stable=0
      if [ -n "${gw:-}" ]; then
        printf '%s\n' "$route_line" | grep -Eq "(^| )via ${gw}($| )" || stable=0
      fi
    elif [ -n "$route_line" ]; then
      stable=0
    fi
  done

  [ "$stable" = "1" ] && return 0
  ./restore_direct.sh --keep-transports || true
  printf '%s\n' "$identity" > run/direct-baseline.identity
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
load_cached_control_endpoint
log "managed linux agent starting pid=$$ control=${VPN_CONTROL_URL}"

while true; do
  mkdir -p run logs transports
  load_cached_control_endpoint
  cycle_ok=0
  control_online=0
  if ensure_tunnel; then
    control_online=1
    if [ "$AGENT_AUTO_UPDATE" = "1" ] && [ -x ./update_agent.sh ]; then
      set +e
      ./update_agent.sh --from-agent
      update_rc=$?
      set -e
      if [ "$update_rc" -eq 10 ]; then
        log "self-update started; exiting current agent process"
        exit 0
      elif [ "$update_rc" -ne 0 ]; then
        log "self-update check failed rc=$update_rc"
      fi
    fi
    if python3 ./route_agent.py config --json > run/fresh-config.json.tmp; then
      mv run/fresh-config.json.tmp run/fresh-config.json
      cache_authenticated_control_endpoint run/fresh-config.json
      apply_config run/fresh-config.json "$control_online"
      cycle_ok=1
    else
      log "config fetch failed"
    fi
  else
    log "control tunnel failed; trying cached policy"
    if python3 ./route_agent.py config --cached --json > run/fresh-config.json.tmp; then
      mv run/fresh-config.json.tmp run/fresh-config.json
      cache_authenticated_control_endpoint run/fresh-config.json
      apply_config run/fresh-config.json "$control_online"
      cycle_ok=1
    else
      log "cached config unavailable"
    fi
  fi
  [ "${RUN_ONCE:-0}" = "1" ] && exit $((1 - cycle_ok))
  sleep "$POLL_SECONDS"
done
