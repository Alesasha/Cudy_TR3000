#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f ./agent.env ]; then
  echo "ERROR: agent.env is missing. Unpack a generated agent bundle first." >&2
  exit 1
fi

run_smoke=1
strict_smoke=0
for arg in "$@"; do
  case "$arg" in
    --skip-smoke)
      run_smoke=0
      ;;
    --strict-smoke)
      strict_smoke=1
      ;;
    *)
      echo "ERROR: unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

mkdir -p run logs transports
chmod +x ./*.sh
if [ -f ./runtime/sing-box ]; then
  chmod +x ./runtime/sing-box || true
fi

fix_workdir_permissions() {
  if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    if [ -n "${SUDO_UID:-}" ] && [ -n "${SUDO_GID:-}" ]; then
      chown -R "$SUDO_UID:$SUDO_GID" run logs transports 2>/dev/null || true
    else
      chown -R "$SUDO_USER" run logs transports 2>/dev/null || true
    fi
  fi
}

on_error() {
  code=$?
  echo
  echo "ERROR: one-click install failed with exit code $code." >&2
  echo "Diagnostic snapshot follows. Send this whole output for analysis." >&2
  if [ -x ./status.sh ]; then
    ./status.sh || true
  fi
  echo
  echo "If internet is broken, run: sudo ./restore_direct.sh" >&2
  exit "$code"
}
trap on_error ERR
trap fix_workdir_permissions EXIT

stop_all_managed_transports() {
  shopt -s nullglob
  for pid_file in run/*.pid; do
    name="$(basename "$pid_file" .pid)"
    [ "$name" = "control-tunnel" ] && continue
    ./stop_singbox_transport.sh "$name" || true
  done
  for config_file in transports/*.json; do
    name="$(basename "$config_file" .json)"
    ./stop_singbox_transport.sh "$name" || true
  done
  shopt -u nullglob
}

echo "== stop stale managed transports =="
stop_all_managed_transports

echo "== restore direct baseline before install =="
./restore_direct.sh || true

missing=0
for cmd in python3 curl ip ssh tar; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command is missing: $cmd" >&2
    missing=1
  fi
done
if ! command -v sing-box >/dev/null 2>&1 && [ ! -x ./runtime/sing-box ]; then
  if [ "${AUTO_INSTALL_SINGBOX:-1}" = "1" ] && [ -x ./install_singbox_runtime.sh ]; then
    echo
    echo "== install bundled sing-box runtime =="
    if ! python3 - <<'PY'
import socket
socket.getaddrinfo("api.github.com", 443)
PY
    then
      echo "ERROR: DNS cannot resolve api.github.com after direct baseline restore." >&2
      echo "Run './status.sh' and send the output, or place sing-box into ./runtime/sing-box and rerun." >&2
      missing=1
    elif ! ./install_singbox_runtime.sh; then
      echo "ERROR: automatic sing-box install failed." >&2
      missing=1
    fi
  else
    echo "ERROR: sing-box is missing. Put it into ./runtime/sing-box or install it in PATH." >&2
    missing=1
  fi
fi
[ "$missing" = "0" ] || exit 1

if [ "$run_smoke" = "1" ]; then
  echo
  echo "== one-shot managed agent smoke =="
  if ! RUN_ONCE=1 ./managed_agent.sh; then
    echo "WARNING: one-shot managed agent smoke failed." >&2
    echo "The systemd service will still be installed and will keep retrying in the background." >&2
    ./restore_direct.sh || true
    if [ "$strict_smoke" = "1" ]; then
      exit 1
    fi
  fi
fi

echo
echo "== install systemd service =="
sudo ./install_systemd.sh "${SERVICE_NAME:-cudy-managed-agent.service}"

echo
./status.sh
