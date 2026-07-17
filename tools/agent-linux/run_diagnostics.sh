#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

[ -f ./agent.env ] && . ./agent.env

strip_cr() {
  printf '%s' "$1" | tr -d '\r'
}

CONTROL_LOCAL_PORT="$(strip_cr "${CONTROL_LOCAL_PORT:-18765}")"
VPN_CONTROL_URL="$(strip_cr "${VPN_CONTROL_URL:-http://127.0.0.1:${CONTROL_LOCAL_PORT}}")"
VPN_AGENT_TOKEN="$(strip_cr "${VPN_AGENT_TOKEN:-}")"
VPN_AGENT_DEVICE_ID="$(strip_cr "${VPN_AGENT_DEVICE_ID:-unknown-linux}")"
MAIL_TO="${DIAGNOSTIC_MAIL_TO:-isasha@list.ru}"

mkdir -p logs run
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
report_path="logs/diagnostic-${VPN_AGENT_DEVICE_ID}-${timestamp}.txt"

wait_for_control_tunnel() {
  if curl -fsS --connect-timeout 3 --max-time 5 "${VPN_CONTROL_URL}/healthz" >/dev/null 2>&1; then
    return 0
  fi
  for _ in $(seq 1 25); do
    if curl -fsS --connect-timeout 3 --max-time 5 "${VPN_CONTROL_URL}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

section() {
  printf '\n== %s ==\n' "$1"
}

run_cmd() {
  printf '\n$ %s\n' "$*"
  "$@" 2>&1 || true
}

copy_report_to_clipboard() {
  if command -v wl-copy >/dev/null 2>&1; then
    wl-copy <"$report_path" >/dev/null 2>&1 && return 0
  fi
  if command -v xclip >/dev/null 2>&1; then
    xclip -selection clipboard <"$report_path" >/dev/null 2>&1 && return 0
  fi
  if command -v xsel >/dev/null 2>&1; then
    xsel --clipboard --input <"$report_path" >/dev/null 2>&1 && return 0
  fi
  return 1
}

{
  section "summary"
  echo "device=${VPN_AGENT_DEVICE_ID}"
  echo "date_utc=$(date -u -Is)"
  echo "date_local=$(date -Is)"
  echo "user=$(id)"
  echo "pwd=$(pwd)"

  section "system"
  run_cmd uname -a
  if command -v hostnamectl >/dev/null 2>&1; then run_cmd hostnamectl; fi
  if command -v uptime >/dev/null 2>&1; then run_cmd uptime; fi

  section "network-manager"
  if command -v nmcli >/dev/null 2>&1; then
    run_cmd nmcli general status
    run_cmd nmcli device status
    run_cmd nmcli -f active,ssid,bssid,chan,freq,signal,security dev wifi
  else
    echo "nmcli is not available"
  fi

  section "agent status"
  if [ -x ./status.sh ]; then
    ./status.sh 2>&1 || true
  else
    echo "status.sh is missing"
  fi

  section "control config dry-run"
  if curl -fsS --connect-timeout 3 --max-time 5 "${VPN_CONTROL_URL}/healthz" >/dev/null 2>&1; then
    python3 ./route_agent.py config --json 2>&1 | head -200 || true
    python3 ./route_agent.py plan --dry-run 2>&1 | head -240 || true
  else
    echo "control server is not reachable at ${VPN_CONTROL_URL}"
  fi

  section "recent journal"
  if command -v journalctl >/dev/null 2>&1; then
    journalctl -n 180 --no-pager 2>&1 | grep -iE 'cudy|amnezia|wireguard|sing-box|zapret|nfqws|NetworkManager|wifi|wlp|dns|resolved|suspend|resume|sleep|wake|drop|reject|denied|error|failed' | tail -120 || true
  else
    echo "journalctl is not available"
  fi
} >"$report_path"

summary="diagnostic ${VPN_AGENT_DEVICE_ID} $(date -Is)"
send_ok=0
if [ -n "$VPN_AGENT_TOKEN" ] && wait_for_control_tunnel; then
  if python3 - "$VPN_CONTROL_URL" "$VPN_AGENT_TOKEN" "$summary" "$report_path" <<'PY'
import json
import sys
import urllib.request

base, token, summary, path = sys.argv[1:5]
report = open(path, encoding="utf-8", errors="replace").read()
payload = json.dumps({"summary": summary, "report": report}).encode("utf-8")
request = urllib.request.Request(
    base.rstrip("/") + "/api/agent/diagnostics",
    data=payload,
    headers={
        "authorization": "Bearer " + token,
        "content-type": "application/json",
    },
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(response.read().decode("utf-8"))
PY
  then
    send_ok=1
  fi
fi

if [ "$send_ok" = "1" ]; then
  echo "Diagnostic report sent to control-server."
else
  echo "Diagnostic report was saved locally but was not sent to control-server."
  echo "Report file: $report_path"
  mail_subject="Cudy diagnostic ${VPN_AGENT_DEVICE_ID}"
  mail_body="Cudy diagnostic report is saved at: $(pwd)/$report_path"
  if command -v mail >/dev/null 2>&1; then
    mail -s "$mail_subject" "$MAIL_TO" <"$report_path" >/dev/null 2>&1 || true
  elif command -v sendmail >/dev/null 2>&1; then
    {
      printf 'To: %s\n' "$MAIL_TO"
      printf 'Subject: %s\n' "$mail_subject"
      printf 'Content-Type: text/plain; charset=UTF-8\n'
      printf '\n'
      cat "$report_path"
    } | sendmail -t >/dev/null 2>&1 || true
  elif command -v xdg-email >/dev/null 2>&1; then
    xdg-email --subject "$mail_subject" --body "$mail_body" "$MAIL_TO" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    python3 - "$MAIL_TO" "$mail_body" <<'PY' | xargs -r xdg-open >/dev/null 2>&1 || true
import sys
from urllib.parse import quote
to, body = sys.argv[1:3]
print(f"mailto:{to}?subject={quote('Cudy diagnostic')}&body={quote(body)}")
PY
  fi
fi

clipboard_ok=0
if copy_report_to_clipboard; then
  clipboard_ok=1
  echo "Diagnostic report copied to clipboard."
else
  echo "Clipboard copy is not available. Report file: $report_path"
fi

if command -v zenity >/dev/null 2>&1 && [ -n "${DISPLAY:-}" ]; then
  display_path="run/diagnostic-display-${timestamp}.txt"
  {
    if [ "$send_ok" = "1" ]; then
      echo "Diagnostic report sent to control-server."
    else
      echo "Diagnostic report was saved locally but was not sent to control-server."
    fi
    echo "Report file: $(pwd)/$report_path"
    if [ "$clipboard_ok" = "1" ]; then
      echo "Report text was copied to clipboard."
    else
      echo "Clipboard copy is not available. Select text in this window or send the report file above."
    fi
    echo
    cat "$report_path"
  } >"$display_path"
  zenity --text-info --title="Cudy Agent Diagnostics" --width=1000 --height=760 --filename="$display_path" 2>/dev/null || cat "$display_path"
else
  cat "$report_path"
fi
