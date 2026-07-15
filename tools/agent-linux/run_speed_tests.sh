#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

[ -f ./agent.env ] && . ./agent.env

mkdir -p logs run
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
report_path="logs/speed-test-${VPN_AGENT_DEVICE_ID:-unknown-linux}-${timestamp}.txt"
summary_path="run/speed-summary-${timestamp}.tsv"
RUN_DEFAULTS=1
QUICK_ONLY=0
CUSTOM_URLS=()

usage() {
  cat <<'EOF'
Usage:
  ./run_speed_tests.sh
  ./run_speed_tests.sh --quick
  ./run_speed_tests.sh --url https://example.com/
  ./run_speed_tests.sh --only-url https://example.com/
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --quick)
      RUN_DEFAULTS=0
      QUICK_ONLY=1
      shift
      ;;
    --url)
      CUSTOM_URLS+=("${2:-}")
      shift 2
      ;;
    --only-url)
      RUN_DEFAULTS=0
      CUSTOM_URLS+=("${2:-}")
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      CUSTOM_URLS+=("$1")
      shift
      ;;
  esac
done

if [ -n "${SPEED_TEST_URL:-}" ]; then
  CUSTOM_URLS+=("$SPEED_TEST_URL")
fi

strip_cr() {
  printf '%s' "$1" | tr -d '\r'
}

CONTROL_LOCAL_PORT="$(strip_cr "${CONTROL_LOCAL_PORT:-18765}")"
VPN_CONTROL_URL="$(strip_cr "${VPN_CONTROL_URL:-http://127.0.0.1:${CONTROL_LOCAL_PORT}}")"
VPN_AGENT_TOKEN="$(strip_cr "${VPN_AGENT_TOKEN:-}")"
VPN_AGENT_DEVICE_ID="$(strip_cr "${VPN_AGENT_DEVICE_ID:-unknown-linux}")"

section() {
  printf '\n== %s ==\n' "$1"
}

resolve_first_v4() {
  local host="$1"
  getent ahostsv4 "$host" 2>/dev/null | awk '{print $1; exit}'
}

route_to_host() {
  local host="$1"
  local ip
  ip="$(resolve_first_v4 "$host" || true)"
  if [ -n "$ip" ]; then
    echo "$host -> $ip"
    ip route get "$ip" 2>&1 || true
  else
    echo "$host -> resolve failed"
  fi
}

route_iface_for_host() {
  local host="$1"
  local ip
  ip="$(resolve_first_v4 "$host" || true)"
  if [ -n "$ip" ]; then
    ip route get "$ip" 2>/dev/null | awk '
      {
        for (i = 1; i <= NF; i++) {
          if ($i == "dev" && (i + 1) <= NF) {
            print $(i + 1)
            exit
          }
        }
      }'
  fi
}

write_probe_summary() {
  local label="$1"
  local kind="$2"
  local host="$3"
  local route_iface="$4"
  local status="$5"
  local http_code="$6"
  local bytes="$7"
  local speed_mbps="$8"
  local total="$9"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$label" "$kind" "$host" "$route_iface" "$status" "$http_code" "$bytes" "$speed_mbps" "$total" >>"$summary_path"
}

curl_probe() {
  local label="$1"
  local url="$2"
  local max_time="${3:-30}"
  local output_target="${4:--o}"
  local kind="${5:-service-page}"
  local output_path="/dev/null"
  local remote_host
  local route_iface
  local status="ok"
  local http_code=""
  local bytes=""
  local total=""
  local speed_mbps=""
  remote_host="$(python3 - "$url" <<'PY'
import sys
from urllib.parse import urlparse
print(urlparse(sys.argv[1]).hostname or "")
PY
)"
  section "$label"
  echo "purpose=${kind}"
  echo "target_url=${url}"
  if [ -n "$remote_host" ]; then
    route_to_host "$remote_host"
    route_iface="$(route_iface_for_host "$remote_host" || true)"
    echo "route_interface=${route_iface:-unknown}"
  fi
  if [ "$output_target" = "body" ]; then
    output_path="run/speed-${label//[^A-Za-z0-9_.-]/_}.body"
  fi
  raw="$(curl -4 -L -sS \
    --connect-timeout 10 \
    --max-time "$max_time" \
    -o "$output_path" \
    -w 'remote_ip=%{remote_ip}\nhttp=%{http_code}\nbytes=%{size_download}\nspeed_Bps=%{speed_download}\nttfb=%{time_starttransfer}\ntotal=%{time_total}\n' \
    "$url" 2>&1 || true)"
  printf '%s\n' "$raw"
  speed_bps="$(printf '%s\n' "$raw" | awk -F= '/^speed_Bps=/{print $2; exit}')"
  http_code="$(printf '%s\n' "$raw" | awk -F= '/^http=/{print $2; exit}')"
  bytes="$(printf '%s\n' "$raw" | awk -F= '/^bytes=/{print $2; exit}')"
  total="$(printf '%s\n' "$raw" | awk -F= '/^total=/{print $2; exit}')"
  if printf '%s\n' "$raw" | grep -Eiq 'timed out|timeout was reached|operation timed out'; then
    status="timeout"
  elif printf '%s\n' "$raw" | grep -Eq '^curl: \([0-9]+\)'; then
    status="curl_error"
  elif [ "${http_code:-000}" = "000" ]; then
    status="no_http"
  fi
  if [ -n "${speed_bps:-}" ]; then
    speed_mbps="$(awk -v bps="$speed_bps" 'BEGIN { printf "%.2f", bps * 8 / 1000000 }')"
    echo "speed_Mbps=${speed_mbps}"
  fi
  if [ "$output_path" != "/dev/null" ]; then
    if grep -Eiq "isn't currently supported in your country|not available in your country|unsupported country" "$output_path" 2>/dev/null; then
      echo "semantic=geo_block"
    else
      echo "semantic=ok_or_unknown"
    fi
  fi
  write_probe_summary "$label" "$kind" "${remote_host:-unknown}" "${route_iface:-unknown}" "$status" "${http_code:-}" "${bytes:-}" "${speed_mbps:-}" "${total:-}"
}

post_report() {
  if [ -z "$VPN_AGENT_TOKEN" ]; then
    return 1
  fi
  if ! curl -fsS --connect-timeout 3 --max-time 5 "${VPN_CONTROL_URL}/healthz" >/dev/null 2>&1; then
    return 1
  fi
  python3 - "$VPN_CONTROL_URL" "$VPN_AGENT_TOKEN" "$report_path" <<'PY'
import json
import sys
import urllib.request

base, token, path = sys.argv[1:4]
report = open(path, encoding="utf-8", errors="replace").read()
payload = json.dumps({"summary": "Linux speed test", "report": report}).encode("utf-8")
request = urllib.request.Request(
    base.rstrip("/") + "/api/agent/diagnostics",
    data=payload,
    headers={"authorization": "Bearer " + token, "content-type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(response.read().decode("utf-8"))
PY
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
  echo "date_local=$(date -Is)"
  echo "control=${VPN_CONTROL_URL}"
  echo
  echo "What this measures:"
  echo "- Baseline Speed Test downloads a reference file from mirror.yandex.ru over the current direct route."
  echo "- Full Speed Tests measure named targets separately: direct reference mirrors, selected service pages, and one CDN reference."
  echo "- Small service pages such as Telegram Web are reachability checks; their Mbps value is not a real throughput benchmark."
  echo "- Each section shows target_url and route_interface so it is clear which server/path was tested."

  if [ "$RUN_DEFAULTS" = "1" ]; then
    section "public ip"
    curl -4 -sS --connect-timeout 5 --max-time 10 https://ifconfig.me/ip || true
    echo

    section "current default routes"
    ip -4 route show default || true
    ip -4 route show 0.0.0.0/1 2>/dev/null || true
    ip -4 route show 128.0.0.0/1 2>/dev/null || true
  fi

  if [ "$QUICK_ONLY" = "1" ]; then
    curl_probe "baseline_direct_yandex_mirror_reference" "http://mirror.yandex.ru/linuxmint-packages/pool/main/m/mint-y-icons/mint-y-icons_1.8.6_all.deb" 60 --o "direct-reference-download"
  fi

  for custom_url in "${CUSTOM_URLS[@]}"; do
    [ -n "$custom_url" ] || continue
    curl_probe "custom_${custom_url//[^A-Za-z0-9_.-]/_}" "$custom_url" 45 body "custom-url-page"
  done

  if [ "$RUN_DEFAULTS" = "1" ]; then
    curl_probe "reference_direct_yandex_mirror_download" "http://mirror.yandex.ru/linuxmint-packages/pool/main/m/mint-y-icons/mint-y-icons_1.8.6_all.deb" 60 --o "direct-reference-download"
    curl_probe "reference_cloudflare_cdn_25mb" "https://speed.cloudflare.com/__down?bytes=25000000" 20 --o "cdn-reference-download"
    curl_probe "service_telegram_web_reachability" "https://web.telegram.org/" 25 --o "service-reachability"
    curl_probe "service_gemini_page" "https://gemini.google.com/" 30 body "service-page"
    curl_probe "service_chatgpt_page" "https://chatgpt.com/" 30 body "service-page"
  fi

  section "readable result table"
  printf '%-40s %-28s %-20s %-12s %-10s %-6s %-12s %-10s %-8s\n' "test" "meaning" "host" "route" "status" "http" "bytes" "mbps" "seconds"
  if [ -s "$summary_path" ]; then
    awk -F '\t' '{
      printf "%-40s %-28s %-20s %-12s %-10s %-6s %-12s %-10s %-8s\n", $1, $2, $3, $4, $5, $6, $7, $8, $9
    }' "$summary_path"
  fi
} >"$report_path"

echo "Speed test report: $report_path"
if post_report >/dev/null 2>&1; then
  echo "Speed test report sent to control-server."
else
  echo "Speed test report was not sent to control-server."
fi
if copy_report_to_clipboard; then
  echo "Speed test report copied to clipboard."
fi

if command -v zenity >/dev/null 2>&1 && [ -n "${DISPLAY:-}" ]; then
  zenity --text-info --title="Cudy Agent Speed Test" --width=1000 --height=760 --filename="$report_path" 2>/dev/null || cat "$report_path"
else
  cat "$report_path"
fi
