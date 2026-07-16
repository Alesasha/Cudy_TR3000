#!/usr/bin/env python3
"""Install the uswest host return-route required for SSH through its AWG container."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import stat
import sys
from pathlib import Path

import harden_control_ssh as ssh_tools


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PASSWORD_FILE = ROOT / "secrets" / "control_backup_ssh_password.txt"


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def password_value(explicit: str, password_file: Path) -> str:
    if explicit:
        return explicit
    for name in ("USWEST_SSH_PASSWORD", "CONTROL_BACKUP_SSH_PASSWORD"):
        value = os.environ.get(name)
        if value:
            return value
    if password_file.exists():
        value = password_file.read_text(encoding="utf-8-sig").strip()
        if value:
            return value
    return getpass.getpass("uswest root password: ")


def remote_route_script(args: argparse.Namespace) -> str:
    container = shlex.quote(args.container)
    docker_network = json.dumps(args.docker_network)
    host_interface = shlex.quote(args.host_interface)
    client_cidr = shlex.quote(args.client_cidr)
    probe_client_ip = shlex.quote(args.probe_client_ip)
    return f"""#!/bin/sh
set -eu
container_ip="$(docker inspect --format '{{{{(index .NetworkSettings.Networks {docker_network}).IPAddress}}}}' {container})"
if [ -z "$container_ip" ]; then
  echo "AWG container address is unavailable" >&2
  exit 1
fi
ip link show dev {host_interface} >/dev/null
ip -4 route replace {client_cidr} via "$container_ip" dev {host_interface}
ip -4 route get {probe_client_ip}
"""


def service_unit() -> str:
    return """[Unit]
Description=Cudy private management route through the AmneziaWG container
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/cudy-awg-private-management-route
"""


def timer_unit(interval_seconds: int) -> str:
    return f"""[Unit]
Description=Refresh Cudy private management route

[Timer]
OnBootSec=30s
OnUnitActiveSec={interval_seconds}s
Unit=cudy-awg-private-management-route.service

[Install]
WantedBy=timers.target
"""


def upload_text(client: object, remote_path: str, content: str, mode: int) -> None:
    sftp = client.open_sftp()
    try:
        with sftp.file(remote_path, "w") as handle:
            handle.write(content)
        sftp.chmod(remote_path, mode)
    finally:
        sftp.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="95.182.91.203")
    parser.add_argument("--user", default="root")
    parser.add_argument("--ssh-password", default="")
    parser.add_argument("--password-file", type=Path, default=DEFAULT_PASSWORD_FILE)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--connect-attempts", type=int, default=3)
    parser.add_argument("--container", default="amnezia-awg2")
    parser.add_argument("--docker-network", default="amnezia-dns-net")
    parser.add_argument("--host-interface", default="amn0")
    parser.add_argument("--client-cidr", default="10.8.1.0/24")
    parser.add_argument("--probe-client-ip", default="10.8.1.10")
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--check-only", action="store_true")
    return parser


def main() -> int:
    configure_stdio()
    args = build_parser().parse_args()
    password = password_value(args.ssh_password, args.password_file)
    client = ssh_tools.connect(
        args.host,
        args.user,
        password,
        args.timeout,
        attempts=args.connect_attempts,
    )
    try:
        if not args.check_only:
            upload_text(
                client,
                "/usr/local/sbin/cudy-awg-private-management-route",
                remote_route_script(args),
                stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
            )
            upload_text(
                client,
                "/etc/systemd/system/cudy-awg-private-management-route.service",
                service_unit(),
                stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH,
            )
            upload_text(
                client,
                "/etc/systemd/system/cudy-awg-private-management-route.timer",
                timer_unit(args.interval_seconds),
                stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH,
            )
            ssh_tools.ssh_exec(
                client,
                "systemctl daemon-reload && systemctl enable --now cudy-awg-private-management-route.timer >/dev/null && systemctl restart cudy-awg-private-management-route.service",
                timeout=max(args.timeout, 90),
            )

        command = (
            "systemctl is-active cudy-awg-private-management-route.timer; "
            "test \"$(systemctl show cudy-awg-private-management-route.service -p Result --value)\" = success; "
            "echo SERVICE_RESULT=success; "
            f"ip -4 route get {shlex.quote(args.probe_client_ip)}; "
            f"ip -4 -o address show dev {shlex.quote(args.host_interface)} | "
            "awk '{split($4,a,\"/\"); print \"PRIVATE_MANAGEMENT_HOST=\" a[1]; exit}'"
        )
        print(ssh_tools.ssh_exec(client, command, timeout=args.timeout), end="")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
