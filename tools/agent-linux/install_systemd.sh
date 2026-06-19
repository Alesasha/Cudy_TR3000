#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$0" "$@"
fi

agent_dir="$(pwd)"
service_name="${1:-cudy-managed-agent.service}"
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

chmod +x "${agent_dir}"/*.sh
systemctl daemon-reload
systemctl enable "${service_name}"
systemctl restart "${service_name}"
systemctl --no-pager --full status "${service_name}" || true
