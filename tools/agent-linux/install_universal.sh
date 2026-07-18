#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
chmod +x ./*.sh

code="${1:-}"
device_id="${2:-}"
display_name="${3:-Linux PC}"

echo "== activate device =="
./enroll_agent.sh "$code" "$device_id" "$display_name"

echo
echo "== install agent in OFF state =="
sudo ./one_click_install.sh --skip-smoke

echo
echo "Install complete. Open the Cudy Agent shortcut and choose ON when ready."
