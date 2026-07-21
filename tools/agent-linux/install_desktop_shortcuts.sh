#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

agent_dir="$(pwd)"

shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

desktop_dir="${XDG_DESKTOP_DIR:-}"
if [ -z "$desktop_dir" ] && command -v xdg-user-dir >/dev/null 2>&1; then
  desktop_dir="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
fi
if [ -z "$desktop_dir" ] || [ "$desktop_dir" = "$HOME" ]; then
  desktop_dir="$HOME/Desktop"
fi
apps_dir="$HOME/.local/share/applications"
mkdir -p "$desktop_dir" "$apps_dir"

write_launcher() {
  local file="$1"
  local name="$2"
  local script="$3"
  local comment="$4"
  local target="$5"
  local terminal="${6:-true}"
  local quoted_dir quoted_script
  quoted_dir="$(shell_quote "$agent_dir")"
  quoted_script="$(shell_quote "./$script")"
  local exec_line
  if [ "$terminal" = "false" ]; then
    exec_line="bash -lc \"cd $quoted_dir && $quoted_script\""
  else
    exec_line="bash -lc \"cd $quoted_dir && $quoted_script; echo; read -r -p 'Press Enter to close...'\""
  fi
  cat >"$target" <<EOF
[Desktop Entry]
Type=Application
Name=$name
Comment=$comment
Terminal=$terminal
Exec=$exec_line
Categories=Network;
StartupWMClass=CudyAgent
EOF
  chmod +x "$target"
  if command -v gio >/dev/null 2>&1; then
    gio set "$target" metadata::trusted true >/dev/null 2>&1 || true
  fi
}

write_launcher cudy-agent.desktop "Cudy Agent" "cudy_agent_ui.sh" "Open Cudy Agent control UI" "$desktop_dir/cudy-agent.desktop" "false"
write_launcher cudy-agent-on.desktop "Cudy Agent ON" "agent_on.sh" "Start Cudy managed route agent" "$desktop_dir/cudy-agent-on.desktop" "true"
write_launcher cudy-agent-off.desktop "Cudy Agent OFF" "agent_off.sh" "Stop Cudy managed route agent and restore direct routing" "$desktop_dir/cudy-agent-off.desktop" "true"
write_launcher cudy-agent-status.desktop "Cudy Agent Status" "status.sh" "Show Cudy managed route agent status" "$desktop_dir/cudy-agent-status.desktop" "true"

cp "$desktop_dir/cudy-agent.desktop" "$apps_dir/cudy-agent.desktop"
cp "$desktop_dir/cudy-agent-on.desktop" "$apps_dir/cudy-agent-on.desktop"
cp "$desktop_dir/cudy-agent-off.desktop" "$apps_dir/cudy-agent-off.desktop"
cp "$desktop_dir/cudy-agent-status.desktop" "$apps_dir/cudy-agent-status.desktop"

echo "Desktop launchers installed:"
echo "  $desktop_dir/cudy-agent.desktop"
echo "  $desktop_dir/cudy-agent-on.desktop"
echo "  $desktop_dir/cudy-agent-off.desktop"
echo "  $desktop_dir/cudy-agent-status.desktop"
