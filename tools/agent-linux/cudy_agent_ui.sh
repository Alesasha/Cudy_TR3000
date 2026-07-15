#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

service_name="cudy-managed-agent.service"
if [ -f ./agent.env ]; then
  # shellcheck disable=SC1091
  . ./agent.env
  service_name="${SERVICE_NAME:-cudy-managed-agent.service}"
fi

status_text() {
  local active enabled
  active="$(systemctl is-active "$service_name" 2>/dev/null || true)"
  enabled="$(systemctl is-enabled "$service_name" 2>/dev/null || true)"
  printf 'Service: %s\nAutostart: %s\n\n' "${active:-unknown}" "${enabled:-unknown}"
  if curl -fsS --connect-timeout 2 --max-time 4 "http://127.0.0.1:${CONTROL_LOCAL_PORT:-18765}/healthz" 2>/dev/null; then
    printf '\nControl tunnel: OK\n'
  else
    printf 'Control tunnel: not connected\n'
  fi
}

run_action() {
  case "$1" in
    on)
      ./agent_on.sh
      ;;
    off)
      ./agent_off.sh
      ;;
    status)
      ./status.sh
      ;;
    diagnostics)
      ./run_diagnostics.sh
      ;;
    settings)
      ./open_user_ui.sh
      ;;
  esac
}

terminal_menu() {
  while true; do
    clear || true
    echo "Cudy Agent"
    echo "=========="
    status_text || true
    echo
    echo "1) ON"
    echo "2) OFF"
    echo "3) Status"
    echo "4) Diagnostics"
    echo "5) Settings"
    echo "0) Exit"
    echo
    read -r -p "Select: " choice
    case "$choice" in
      1) run_action on ;;
      2) run_action off ;;
      3) run_action status ;;
      4) run_action diagnostics ;;
      5) run_action settings ;;
      0) exit 0 ;;
      *) echo "Unknown choice." ;;
    esac
    echo
    read -r -p "Press Enter to continue..." _
  done
}

if command -v python3 >/dev/null 2>&1 && [ -n "${DISPLAY:-}" ]; then
  if python3 - <<'PY' >/dev/null 2>&1
import tkinter  # noqa: F401
PY
  then
    exec python3 ./cudy_agent_ui.py
  fi
fi

if command -v zenity >/dev/null 2>&1 && [ -n "${DISPLAY:-}" ]; then
  while true; do
    summary="$(status_text 2>/dev/null || true)"
    choice="$(zenity --list --title="Cudy Agent" --width=720 --height=520 --text="$summary" --column="Action" \
      "ON" "OFF" "Status" "Diagnostics" "Settings" "Exit" 2>/dev/null || true)"
    case "$choice" in
      ON) run_action on ;;
      OFF) run_action off ;;
      Status) ./status.sh | zenity --text-info --title="Cudy Agent Status" --width=900 --height=700 2>/dev/null || run_action status ;;
      Diagnostics) run_action diagnostics ;;
      Settings) run_action settings ;;
      Exit|"") exit 0 ;;
    esac
  done
else
  terminal_menu
fi
