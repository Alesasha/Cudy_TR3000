#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$0" "$@"
fi

agent_dir="$(pwd)"
service_name="${1:-cudy-managed-agent.service}"
service_base="${service_name%.service}"
watchdog_service="${service_base}-watchdog.service"
watchdog_timer="${service_base}-watchdog.timer"
cat >"/etc/systemd/system/${service_name}" <<EOF
[Unit]
Description=Cudy Managed Route Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${agent_dir}
ExecStart=${agent_dir}/managed_agent.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat >"/etc/systemd/system/${watchdog_service}" <<EOF
[Unit]
Description=Cudy Agent Connectivity Safety Watchdog
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=${agent_dir}
ExecStart=/usr/bin/python3 ${agent_dir}/watch_agent_connectivity.py --agent-service ${service_name}
EOF

cat >"/etc/systemd/system/${watchdog_timer}" <<EOF
[Unit]
Description=Run Cudy Agent Connectivity Safety Watchdog

[Timer]
OnBootSec=60
OnUnitActiveSec=30
AccuracySec=5
Persistent=true
Unit=${watchdog_service}

[Install]
WantedBy=timers.target
EOF

chmod +x "${agent_dir}"/*.sh
chmod +x "${agent_dir}/watch_agent_connectivity.py"
systemctl daemon-reload
systemctl enable "${service_name}"
systemctl enable --now "${watchdog_timer}"
systemctl restart "${service_name}"
systemctl --no-pager --full status "${service_name}" || true
systemctl --no-pager --full status "${watchdog_timer}" || true
