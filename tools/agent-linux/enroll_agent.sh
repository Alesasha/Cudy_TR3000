#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

code="${1:-}"
device_id="${2:-}"
display_name="${3:-Linux PC}"
bootstrap_host="${CUDY_ENROLLMENT_HOST:-95.182.91.203}"
bootstrap_user="${CUDY_ENROLLMENT_USER:-cudy-enroll}"
bootstrap_remote_port="${CUDY_ENROLLMENT_REMOTE_PORT:-8766}"
bootstrap_local_port="${CUDY_ENROLLMENT_LOCAL_PORT:-18766}"
bootstrap_key="./enrollment_bootstrap_ed25519"
host_public_key="./control_ssh_host_ed25519.pub"

if [ -z "$code" ]; then
  printf 'One-time activation code: '
  IFS= read -r code
fi
code="$(printf '%s' "$code" | tr -d '[:space:]')"
[ -n "$code" ] || { echo "ERROR: one-time activation code is required." >&2; exit 2; }

for command in ssh curl python3; do
  command -v "$command" >/dev/null 2>&1 || { echo "ERROR: required command is missing: $command" >&2; exit 1; }
done
for path in "$bootstrap_key" "$host_public_key"; do
  [ -f "$path" ] || { echo "ERROR: enrollment package file is missing: $path" >&2; exit 1; }
done

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/cudy-enrollment.XXXXXX")"
ssh_pid=""
cleanup() {
  [ -z "$ssh_pid" ] || kill "$ssh_pid" >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

read -r host_key_type host_key_data _ < "$host_public_key"
printf '%s %s %s\n' "$bootstrap_host" "$host_key_type" "$host_key_data" > "$tmp_dir/known_hosts"
chmod 600 "$bootstrap_key" "$tmp_dir/known_hosts"

ssh \
  -i "$bootstrap_key" \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o PasswordAuthentication=no \
  -o KbdInteractiveAuthentication=no \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$tmp_dir/known_hosts" \
  -o ExitOnForwardFailure=yes \
  -o ConnectTimeout=12 \
  -o ConnectionAttempts=1 \
  -N -L "127.0.0.1:${bootstrap_local_port}:127.0.0.1:${bootstrap_remote_port}" \
  "${bootstrap_user}@${bootstrap_host}" \
  >"$tmp_dir/ssh.out" 2>"$tmp_dir/ssh.err" &
ssh_pid=$!

ready=0
for _ in $(seq 1 80); do
  if ! kill -0 "$ssh_pid" >/dev/null 2>&1; then
    cat "$tmp_dir/ssh.err" >&2 || true
    exit 1
  fi
  if python3 - "$bootstrap_local_port" <<'PY' >/dev/null 2>&1
import socket, sys
with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=0.25):
    pass
PY
  then
    ready=1
    break
  fi
  sleep 0.25
done
if [ "$ready" != "1" ]; then
  echo "ERROR: enrollment tunnel did not become ready." >&2
  tail -20 "$tmp_dir/ssh.err" >&2 || true
  exit 1
fi

python3 - "$code" "$device_id" "$display_name" > "$tmp_dir/request.json" <<'PY'
import json, sys
print(json.dumps({"code": sys.argv[1], "device_id": sys.argv[2], "display_name": sys.argv[3], "platform": "linux"}))
PY
curl -fsS \
  --connect-timeout 5 \
  --max-time 30 \
  -H 'Content-Type: application/json' \
  --data-binary @"$tmp_dir/request.json" \
  "http://127.0.0.1:${bootstrap_local_port}/api/agent/enroll" \
  > "$tmp_dir/response.json"

python3 - "$tmp_dir/response.json" "$host_public_key" <<'PY'
import json
import os
import shlex
import sys
from pathlib import Path

response_path = Path(sys.argv[1])
host_public_key_path = Path(sys.argv[2])
result = json.loads(response_path.read_text(encoding="utf-8"))
provisioning = result.get("provisioning") or {}
required = {
    "token": result.get("token"),
    "device_id": result.get("device_id"),
    "ssh_host": provisioning.get("ssh_host"),
    "ssh_user": provisioning.get("ssh_user"),
    "ssh_private_key": provisioning.get("ssh_private_key"),
}
missing = [name for name, value in required.items() if not value]
if missing:
    raise SystemExit("Enrollment response is incomplete: " + ", ".join(missing))

host_parts = host_public_key_path.read_text(encoding="ascii").strip().split()
if len(host_parts) < 2:
    raise SystemExit("Invalid control-server host public key")

Path("uswest_control_tunnel_ed25519").write_text(str(required["ssh_private_key"]).rstrip() + "\n", encoding="ascii")
os.chmod("uswest_control_tunnel_ed25519", 0o600)
Path("known_hosts").write_text(f'{required["ssh_host"]} {host_parts[0]} {host_parts[1]}\n', encoding="ascii")
os.chmod("known_hosts", 0o600)

version_code = "1"
try:
    version_code = str(json.loads(Path("agent.version.json").read_text(encoding="utf-8"))["version_code"])
except (OSError, KeyError, ValueError, TypeError):
    pass
values = {
    "VPN_CONTROL_URL": "http://127.0.0.1:18765",
    "VPN_CONTROL_URLS": "http://10.77.0.1:8765,http://192.168.8.1:8765",
    "VPN_CONTROL_ENDPOINT_MANIFEST_URLS": "http://10.77.0.1/cudy-control/endpoints.json,http://192.168.8.1/cudy-control/endpoints.json",
    "VPN_AGENT_TOKEN": required["token"],
    "VPN_AGENT_DEVICE_ID": required["device_id"],
    "AGENT_VERSION_CODE": version_code,
    "AGENT_AUTO_UPDATE": "1",
    "CONTROL_HOST": required["ssh_host"],
    "CONTROL_PORT": "22",
    "CONTROL_USER": required["ssh_user"],
    "CONTROL_LOCAL_PORT": "18765",
    "CONTROL_REMOTE_PORT": "8765",
    "SSH_KEY": "./uswest_control_tunnel_ed25519",
    "KNOWN_HOSTS_FILE": "./known_hosts",
    "POLL_SECONDS": "60",
    "DIRECT_BASELINE": "1",
}
text = "".join(f"{name}={shlex.quote(str(value))}\n" for name, value in values.items())
tmp = Path("agent.env.tmp")
tmp.write_text(text, encoding="utf-8", newline="\n")
os.chmod(tmp, 0o600)
tmp.replace("agent.env")
print(f'Device activated: {required["device_id"]}')
print("Configuration saved. The one-time code cannot be reused.")
PY
