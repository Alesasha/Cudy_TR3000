#!/usr/bin/env python3
"""Install and read the Cudy per-client flow monitor."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

import lokvpn_slots as ssh_tools


ROOT = Path(__file__).resolve().parents[1]
LOCAL_MONITOR = ROOT / "openwrt" / "cudy-client-flow-monitor"
LOCAL_INIT = ROOT / "openwrt" / "cudy-client-flow-monitor.init"
REMOTE_MONITOR = "/usr/bin/cudy-client-flow-monitor"
REMOTE_INIT = "/etc/init.d/cudy-client-flow-monitor"
REMOTE_LOG = "/tmp/cudy-client-flow-monitor.log"


def connect(args: argparse.Namespace):
    password = ssh_tools.load_cudy_ssh_password(args.ssh_password)
    if not password:
        raise RuntimeError("Cudy SSH password is not configured")
    return ssh_tools.ssh_connect(args.ssh_host, args.ssh_user, password, args.ssh_timeout)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ssh-host", default=ssh_tools.DEFAULT_CUDY_HOST)
    parser.add_argument("--ssh-user", default=ssh_tools.DEFAULT_CUDY_USER)
    parser.add_argument("--ssh-password")
    parser.add_argument("--ssh-timeout", type=int, default=60)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Cudy per-client flow monitor.")
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="Install and start monitor service.")
    install.add_argument("--client-ip", default="10.77.0.3")
    install.add_argument("--watch-ips", default="213.180.204.183 89.108.110.121")
    add_common(install)

    status = sub.add_parser("status", help="Show service status and recent log lines.")
    status.add_argument("--lines", type=int, default=30)
    add_common(status)

    tail = sub.add_parser("tail", help="Print recent log lines.")
    tail.add_argument("--lines", type=int, default=120)
    tail.add_argument("--json", action="store_true", help="Return raw lines as JSON list.")
    add_common(tail)

    clear = sub.add_parser("clear", help="Clear monitor log.")
    add_common(clear)

    return parser


def install_monitor(client, args: argparse.Namespace) -> str:
    ssh_tools.ssh_upload_file(client, LOCAL_MONITOR, REMOTE_MONITOR, args.ssh_timeout)
    ssh_tools.ssh_upload_file(client, LOCAL_INIT, REMOTE_INIT, args.ssh_timeout)
    command = (
        f"sed -i "
        f"-e {shlex.quote('s/^    procd_set_param env CLIENT_IP=.*/    procd_set_param env CLIENT_IP=' + args.client_ip + '/')} "
        f"-e {shlex.quote('s/^    procd_set_param env WATCH_IPS=.*/    procd_set_param env WATCH_IPS=\"' + args.watch_ips + '\"/')} "
        f"{shlex.quote(REMOTE_INIT)} && "
        f"sh -n {shlex.quote(REMOTE_MONITOR)} && sh -n {shlex.quote(REMOTE_INIT)} && "
        f"chmod +x {shlex.quote(REMOTE_MONITOR)} {shlex.quote(REMOTE_INIT)} && "
        f"{shlex.quote(REMOTE_INIT)} enable && {shlex.quote(REMOTE_INIT)} restart && sleep 3 && "
        f"{shlex.quote(REMOTE_INIT)} status && tail -n 20 {shlex.quote(REMOTE_LOG)} 2>/dev/null || true"
    )
    return ssh_tools.ssh_exec_checked(client, command, args.ssh_timeout)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        client = connect(args)
    except Exception as exc:
        print(f"SSH connection failed: {exc}", file=sys.stderr)
        return 1
    try:
        if args.command == "install":
            print(install_monitor(client, args))
            return 0
        if args.command == "status":
            command = f"{shlex.quote(REMOTE_INIT)} status; tail -n {int(args.lines)} {shlex.quote(REMOTE_LOG)} 2>/dev/null || true"
            print(ssh_tools.ssh_exec_checked(client, command, args.ssh_timeout))
            return 0
        if args.command == "tail":
            command = f"tail -n {int(args.lines)} {shlex.quote(REMOTE_LOG)} 2>/dev/null || true"
            output = ssh_tools.ssh_exec_checked(client, command, args.ssh_timeout)
            if args.json:
                print(json.dumps(output.splitlines(), ensure_ascii=False, indent=2))
            else:
                print(output)
            return 0
        if args.command == "clear":
            ssh_tools.ssh_exec_checked(client, f": > {shlex.quote(REMOTE_LOG)}", args.ssh_timeout)
            print(f"Cleared {REMOTE_LOG}")
            return 0
    except Exception as exc:
        print(str(exc) or repr(exc), file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
