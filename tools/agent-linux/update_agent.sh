#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

[ -f ./agent.env ] && . ./agent.env

CONTROL_URL="${VPN_CONTROL_URL:-http://127.0.0.1:${CONTROL_LOCAL_PORT:-18765}}"
PLATFORM="${AGENT_PLATFORM:-linux}"
VERSION_FILE="${AGENT_VERSION_FILE:-./agent.version.json}"
WORK_DIR="${AGENT_UPDATE_DIR:-./updates}"
SERVICE_NAME="${AGENT_SERVICE_NAME:-cudy-managed-agent.service}"
UPDATE_STATUS_FILE="${AGENT_UPDATE_STATUS_FILE:-./run/update-status.txt}"
UPDATE_MARKER_FILE="${AGENT_UPDATE_MARKER_FILE:-./run/update-in-progress.json}"
FROM_AGENT=0
FORCE_UPDATE=0
APPLY_STAGE=""
ORIGINAL_ARGS=("$@")

write_update_status() {
  mkdir -p "$(dirname "$UPDATE_STATUS_FILE")" logs >/dev/null 2>&1 || true
  printf '[%s] %s\n' "$(date -Is)" "$*" >"$UPDATE_STATUS_FILE" 2>/dev/null || true
}

write_update_marker() {
  mkdir -p "$(dirname "$UPDATE_MARKER_FILE")" >/dev/null 2>&1 || true
  python3 - "$UPDATE_MARKER_FILE" "$1" <<'PY' 2>/dev/null || true
import json, os, sys, time
path, phase = sys.argv[1:3]
temp = path + ".tmp"
with open(temp, "w", encoding="utf-8") as fh:
    json.dump({"phase": phase, "updated_at_epoch": int(time.time())}, fh)
    fh.write("\n")
os.replace(temp, path)
PY
}

clear_update_marker() {
  rm -f -- "$UPDATE_MARKER_FILE" 2>/dev/null || true
}

on_exit() {
  rc=$?
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 10 ]; then
    write_update_status "failed rc=$rc. Some services may be unavailable until the agent is restarted or turned off/on."
    clear_update_marker
  fi
}
trap on_exit EXIT

while [ "$#" -gt 0 ]; do
  case "$1" in
    --from-agent) FROM_AGENT=1; shift ;;
    --force) FORCE_UPDATE=1; shift ;;
    --apply-staged) APPLY_STAGE="${2:-}"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

rerun_as_root_for_manual_update() {
  local script_path
  script_path="$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")"
  if [ -n "${DISPLAY:-}" ] && command -v pkexec >/dev/null 2>&1; then
    exec pkexec /bin/bash "$script_path" "$@"
  fi
  if [ -t 0 ]; then
    exec sudo "$script_path" "$@"
  fi
  if command -v pkexec >/dev/null 2>&1; then
    exec pkexec /bin/bash "$script_path" "$@"
  fi
  echo "ERROR: root permissions are required to update the agent." >&2
  echo "Run this command from a terminal instead: sudo $script_path" >&2
  exit 1
}

if [ "$(id -u)" -ne 0 ] && [ "$FROM_AGENT" = "0" ]; then
  rerun_as_root_for_manual_update "${ORIGINAL_ARGS[@]}"
fi

current_version_code() {
  if [ -f "$VERSION_FILE" ]; then
    python3 - "$VERSION_FILE" <<'PY'
import json, sys
try:
    print(int(json.load(open(sys.argv[1], encoding="utf-8")).get("version_code") or 0))
except Exception:
    print(0)
PY
    return
  fi
  if [ -n "${AGENT_VERSION_CODE:-}" ]; then
    printf '%s\n' "$AGENT_VERSION_CODE"
    return
  fi
  printf '0\n'
}

wait_for_service_active() {
  local deadline=$((SECONDS + 45))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_control_ready() {
  local deadline=$((SECONDS + 35))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if curl -fsS --connect-timeout 2 --max-time 4 "${CONTROL_URL}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

copy_update_files() {
  local src="$1"
  shopt -s dotglob nullglob
  for item in "$src"/*; do
    name="$(basename "$item")"
    case "$name" in
      agent.env|uswest_control_tunnel_ed25519|uswest_control_tunnel_ed25519.pub|run|logs|transports|updates|runtime)
        continue
        ;;
      *.conf)
        continue
        ;;
    esac
    rm -rf -- "$name"
    cp -a -- "$item" "$name"
  done
  shopt -u dotglob nullglob
}

apply_staged_update() {
  local stage_path="$1"
  local log_file="logs/update-agent.out.log"
  write_update_status "applying staged update. Some services may be temporarily unavailable until update finishes."
  write_update_marker "applying"
  echo "Applying staged agent update from $stage_path" >> "$log_file" 2>/dev/null || true
  if command -v systemctl >/dev/null 2>&1; then
    write_update_status "stopping agent service to apply update. Some services may be temporarily unavailable until update finishes."
    write_update_marker "stopping-service"
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  fi
  write_update_status "installing new agent files. Some services may be temporarily unavailable until update finishes."
  write_update_marker "installing"
  copy_update_files "$stage_path"
  if [ -f "$stage_path/agent.version.json" ]; then
    cp -f "$stage_path/agent.version.json" "$VERSION_FILE"
  fi
  chmod +x ./*.sh 2>/dev/null || true
  echo "Installed version code: $(current_version_code)" >> "$log_file" 2>/dev/null || true
  if command -v systemctl >/dev/null 2>&1; then
    write_update_status "restarting agent service after update."
    write_update_marker "restarting-service"
    if ! ./install_systemd.sh "$SERVICE_NAME" >>"$log_file" 2>&1; then
      echo "install_systemd.sh failed; attempting direct service restart" >>"$log_file"
      systemctl daemon-reload >>"$log_file" 2>&1 || true
      systemctl enable "$SERVICE_NAME" >>"$log_file" 2>&1 || true
      systemctl restart "$SERVICE_NAME" >>"$log_file" 2>&1
    fi
    if ! wait_for_service_active; then
      write_update_status "failed: agent service did not become active after update. Run Diagnostics or turn the agent OFF and ON."
      systemctl --no-pager --full status "$SERVICE_NAME" >>"$log_file" 2>&1 || true
      journalctl -u "$SERVICE_NAME" -n 120 --no-pager >>"$log_file" 2>&1 || true
      return 1
    fi
    if wait_for_control_ready; then
      write_update_status "completed current=$(current_version_code) service=active control=ready."
    else
      write_update_status "completed current=$(current_version_code) service=active control=pending. The agent will keep reconnecting automatically."
    fi
  else
    write_update_status "completed current=$(current_version_code) service=not-managed."
  fi
  rm -rf -- "$WORK_DIR" 2>/dev/null || true
  clear_update_marker
  echo "Agent update applied from $stage_path"
}

start_apply_staged_update() {
  local stage_path="$1"
  local unit_name
  unit_name="cudy-agent-update-$(date +%s)"
  if command -v systemd-run >/dev/null 2>&1; then
    systemd-run \
      --unit="$unit_name" \
      --description="Cudy Agent self-update" \
      --working-directory="$(pwd)" \
      --same-dir \
      --collect \
      /bin/bash ./update_agent.sh --apply-staged "$stage_path" >/dev/null
    echo "Agent update apply task started via systemd-run: $unit_name"
    return 0
  fi
  if command -v setsid >/dev/null 2>&1; then
    nohup setsid "$0" --apply-staged "$stage_path" >logs/update-agent.out.log 2>logs/update-agent.err.log </dev/null &
  else
    nohup "$0" --apply-staged "$stage_path" >logs/update-agent.out.log 2>logs/update-agent.err.log </dev/null &
  fi
  echo "Agent update apply process started."
}

if [ -n "$APPLY_STAGE" ]; then
  apply_staged_update "$APPLY_STAGE"
  exit 0
fi

write_update_status "checking latest version."

manifest_json="$(python3 - "$CONTROL_URL" "$PLATFORM" "${VPN_AGENT_TOKEN:-}" <<'PY'
import json
import sys
import urllib.request

base, platform, token = sys.argv[1:4]
request = urllib.request.Request(base.rstrip("/") + f"/api/agent/app-version?platform={platform}")
if token:
    request.add_header("Authorization", "Bearer " + token)
with urllib.request.urlopen(request, timeout=20) as response:
    print(response.read().decode("utf-8"))
PY
)"

latest_code="$(python3 - "$manifest_json" <<'PY'
import json, sys
print(int(json.loads(sys.argv[1]).get("version_code") or 0))
PY
)"
expected_sha256="$(python3 - "$manifest_json" <<'PY'
import json, sys
print(str(json.loads(sys.argv[1]).get("sha256") or "").lower())
PY
)"
download_url="$(python3 - "$manifest_json" <<'PY'
import json, sys
print(json.loads(sys.argv[1]).get("download_url") or "")
PY
)"
current_code="$(current_version_code)"

if [ "$latest_code" -le "$current_code" ] && [ "$FORCE_UPDATE" = "0" ]; then
  clear_update_marker
  write_update_status "up_to_date current=$current_code latest=$latest_code."
  echo "Agent is up to date: current=$current_code latest=$latest_code"
  exit 0
fi
if [ -z "$download_url" ]; then
  write_update_status "update available but download_url is empty: current=$current_code latest=$latest_code."
  echo "Update available but download_url is empty: current=$current_code latest=$latest_code"
  exit 0
fi

archive="$WORK_DIR/agent-update-${PLATFORM}-${latest_code}.zip"
stage="$WORK_DIR/stage"
staged_code="0"
if [ -f "$stage/agent.version.json" ]; then
  staged_code="$(python3 - "$stage/agent.version.json" <<'PY'
import json, sys
try:
    print(int(json.load(open(sys.argv[1], encoding="utf-8")).get("version_code") or 0))
except Exception:
    print(0)
PY
)"
fi
if [ "$staged_code" -eq "$latest_code" ] && [ "$FORCE_UPDATE" = "0" ]; then
  clear_update_marker
  if [ "$FROM_AGENT" = "1" ]; then
    write_update_status "ready_to_install current=$current_code latest=$latest_code. Waiting for user approval."
    echo "Agent update is already downloaded and waiting for user approval: current=$current_code latest=$latest_code"
    exit 0
  fi
  write_update_status "starting approved update current=$current_code latest=$latest_code. Some services may be temporarily unavailable until update finishes."
  start_apply_staged_update "$stage"
  write_update_status "apply process started current=$current_code latest=$latest_code. Some services may be temporarily unavailable until update finishes."
  echo "Approved staged agent update apply process started: current=$current_code latest=$latest_code"
  exit 10
fi

rm -rf -- "$WORK_DIR"
mkdir -p "$WORK_DIR" logs
write_update_marker "downloading"
write_update_status "downloading update current=$current_code latest=$latest_code. The current agent remains active."
python3 - "$CONTROL_URL" "$download_url" "$archive" "${VPN_AGENT_TOKEN:-}" "$UPDATE_STATUS_FILE" "$current_code" "$latest_code" <<'PY'
import os
import sys
import time
import urllib.request
from datetime import datetime

base, url, output, token, status_path, current_code, latest_code = sys.argv[1:8]
if url.startswith("/"):
    url = base.rstrip("/") + url
request = urllib.request.Request(url)
if token and url.startswith(base.rstrip("/")):
    request.add_header("Authorization", "Bearer " + token)

def write_status(message):
    os.makedirs(os.path.dirname(status_path) or ".", exist_ok=True)
    temp = status_path + ".tmp"
    with open(temp, "w", encoding="utf-8") as fh:
        fh.write(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}\n")
    os.replace(temp, status_path)

with urllib.request.urlopen(request, timeout=180) as response, open(output, "wb") as fh:
    total = int(response.headers.get("Content-Length") or 0)
    received = 0
    last_percent = -1
    last_update = 0.0
    while True:
        chunk = response.read(256 * 1024)
        if not chunk:
            break
        fh.write(chunk)
        received += len(chunk)
        percent = int(received * 100 / total) if total > 0 else -1
        now = time.monotonic()
        if percent >= 0 and (percent >= last_percent + 5 or now - last_update >= 3):
            write_status(
                f"downloading update current={current_code} latest={latest_code} progress={min(percent, 100)}%. "
                "The current agent remains active."
            )
            last_percent = percent
            last_update = now
    write_status(
        f"staging downloaded update current={current_code} latest={latest_code}. "
        "The current agent remains active."
    )
PY
if [ -n "$expected_sha256" ]; then
  actual_sha256="$(sha256sum "$archive" | awk '{print tolower($1)}')"
  if [ "$actual_sha256" != "$expected_sha256" ]; then
    echo "ERROR: downloaded update checksum mismatch" >&2
    exit 1
  fi
fi
rm -rf "$stage"
mkdir -p "$stage"
python3 - "$archive" "$stage" <<'PY'
import sys
import zipfile
archive, target = sys.argv[1:3]
with zipfile.ZipFile(archive) as zf:
    zf.extractall(target)
PY
printf '%s\n' "$manifest_json" > "$stage/agent.version.json"

if [ "$FROM_AGENT" = "1" ]; then
  clear_update_marker
  write_update_status "ready_to_install current=$current_code latest=$latest_code. Waiting for user approval."
  echo "Agent update downloaded and verified; waiting for user approval: current=$current_code latest=$latest_code"
  exit 0
fi

write_update_status "starting update apply current=$current_code latest=$latest_code. Some services may be temporarily unavailable until update finishes."
start_apply_staged_update "$stage"
write_update_status "apply process started current=$current_code latest=$latest_code. Some services may be temporarily unavailable until update finishes."
echo "Agent update downloaded and apply process started: current=$current_code latest=$latest_code"
exit 10
