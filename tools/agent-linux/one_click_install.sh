#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f ./agent.env ]; then
  echo "ERROR: agent.env is missing. Unpack a generated agent bundle first." >&2
  exit 1
fi

run_smoke=0
strict_smoke=0
start_after_install=0
for arg in "$@"; do
  case "$arg" in
    --skip-smoke)
      run_smoke=0
      ;;
    --strict-smoke)
      strict_smoke=1
      ;;
    --smoke)
      run_smoke=1
      ;;
    --start)
      start_after_install=1
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

needs_direct_restore() {
  shopt -s nullglob
  for pid_file in run/*.pid; do
    name="$(basename "$pid_file" .pid)"
    [ "$name" = "control-tunnel" ] && continue
    shopt -u nullglob
    return 0
  done
  shopt -u nullglob
  ip -4 route show 2>/dev/null | grep -Eq ' dev (amn|wg|awg|tun|ppp|sing|proxy|lokvpn)' && return 0
  ip -o link show 2>/dev/null | grep -Eq '^[0-9]+: (amn|wg|awg|tun|ppp|sing|proxy|lokvpn)' && return 0
  return 1
}

cleanup_direct_half_routes() {
  for prefix in 0.0.0.0/1 128.0.0.0/1; do
    while ip -4 route show "$prefix" 2>/dev/null | grep -Eq ' via | dev '; do
      ip route del "$prefix" 2>/dev/null || break
    done
  done
}

python_tk_available() {
  python3 - <<'PY' >/dev/null 2>&1
import tkinter
PY
}

install_optional_ui_dependencies() {
  echo
  echo "== optional desktop UI dependency: python3-tk =="
  if [ "${CUDY_SKIP_UI_DEPS:-0}" = "1" ]; then
    echo "Skipping optional UI dependency check because CUDY_SKIP_UI_DEPS=1."
    return 0
  fi
  if python_tk_available; then
    echo "Python/Tk is available."
    return 0
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "python3-tk is missing, and apt-get was not found."
    echo "The agent still works; desktop UI will fall back to zenity/terminal mode."
    return 0
  fi

  echo "python3-tk is missing. Trying to install it automatically..."
  if sudo DEBIAN_FRONTEND=noninteractive apt-get update \
    && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-tk; then
    if python_tk_available; then
      echo "Python/Tk installed successfully."
    else
      echo "python3-tk install finished, but tkinter is still unavailable."
      echo "The agent still works; desktop UI will fall back to zenity/terminal mode."
    fi
  else
    echo "WARNING: automatic python3-tk installation failed." >&2
    echo "The agent still works; desktop UI will fall back to zenity/terminal mode." >&2
  fi
  return 0
}

echo "== stop stale managed transports =="
stop_all_managed_transports

if command -v systemctl >/dev/null 2>&1; then
  echo "== stop previous service if present =="
  sudo systemctl disable --now "${SERVICE_NAME:-cudy-managed-agent.service}" 2>/dev/null || true
  sudo systemctl reset-failed "${SERVICE_NAME:-cudy-managed-agent.service}" 2>/dev/null || true
fi

if needs_direct_restore; then
  echo "== restore direct baseline before install =="
  ./restore_direct.sh || true
else
  echo "== direct baseline already clean; remove stale direct half-routes only =="
  cleanup_direct_half_routes || true
fi

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

install_optional_ui_dependencies

if [ "$run_smoke" = "1" ]; then
  echo
  echo "== one-shot managed agent smoke =="
  if ! RUN_ONCE=1 ./managed_agent.sh; then
    echo "WARNING: one-shot managed agent smoke failed." >&2
    echo "The agent will remain OFF unless you start it explicitly." >&2
    ./restore_direct.sh || true
    if [ "$strict_smoke" = "1" ]; then
      exit 1
    fi
  fi
fi

echo
echo "== install desktop shortcuts =="
if [ -x ./install_desktop_shortcuts.sh ]; then
  if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    user_home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    sudo -u "$SUDO_USER" env HOME="$user_home" ./install_desktop_shortcuts.sh || true
  else
    ./install_desktop_shortcuts.sh || true
  fi
fi

echo
if [ "$start_after_install" = "1" ]; then
  echo "== start agent service =="
  ./agent_on.sh
else
  echo "== keep agent OFF =="
  if needs_direct_restore; then
    ./agent_off.sh || true
  else
    ./agent_off.sh --no-restore || true
  fi
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl reset-failed "${SERVICE_NAME:-cudy-managed-agent.service}" 2>/dev/null || true
  fi
fi

echo
echo "Install complete. Open the 'Cudy Agent' desktop shortcut to turn the agent on/off or open settings."
echo
./status.sh || true
