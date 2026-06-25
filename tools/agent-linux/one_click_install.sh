#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f ./agent.env ]; then
  echo "ERROR: agent.env is missing. Unpack a generated agent bundle first." >&2
  exit 1
fi

run_smoke=1
if [ "${1:-}" = "--skip-smoke" ]; then
  run_smoke=0
fi

mkdir -p run logs transports
chmod +x ./*.sh

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
    if ! ./install_singbox_runtime.sh; then
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
  RUN_ONCE=1 ./managed_agent.sh
fi

echo
echo "== install systemd service =="
sudo ./install_systemd.sh "${SERVICE_NAME:-cudy-managed-agent.service}"

echo
./status.sh
