#!/usr/bin/env python3
"""Recover and harden uswest SSH through Cudy over a verified private path.

The private SSH address is intentionally required. Do not infer it from an AWG
peer address: client addresses such as 10.8.1.1 are not server management
addresses.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import paramiko

import harden_control_ssh as harden


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CUDY_PASSWORD_FILE = ROOT / "secrets" / "cudy_ssh_password.txt"
DEFAULT_USWEST_PASSWORD_FILE = ROOT / "secrets" / "control_backup_ssh_password.txt"


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def password_value(explicit: str, env_names: tuple[str, ...], password_file: Path, prompt: str) -> str:
    if explicit:
        return explicit
    for name in env_names:
        value = os.environ.get(name)
        if value:
            return value
    if password_file.exists():
        value = password_file.read_text(encoding="utf-8-sig").strip()
        if value:
            return value
    return getpass.getpass(prompt)


def connect_client(
    host: str,
    user: str,
    password: str,
    timeout: int,
    *,
    sock: object | None = None,
) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        username=user,
        password=password,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        look_for_keys=False,
        allow_agent=False,
        sock=sock,
    )
    return client


def connect_via_cudy(args: argparse.Namespace, cudy_password: str, uswest_password: str) -> tuple[paramiko.SSHClient, paramiko.SSHClient]:
    last_error: Exception | None = None
    for attempt in range(1, max(1, args.connect_attempts) + 1):
        cudy: paramiko.SSHClient | None = None
        uswest: paramiko.SSHClient | None = None
        try:
            cudy = connect_client(args.cudy_host, args.cudy_user, cudy_password, args.timeout)
            transport = cudy.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("Cudy SSH transport is not active")
            harden.ssh_exec(
                cudy,
                f"ip -4 route replace {shlex.quote(args.private_host)}/32 dev {shlex.quote(args.cudy_awg_interface)}",
                timeout=args.timeout,
            )
            channel = transport.open_channel(
                "direct-tcpip",
                (args.private_host, args.private_port),
                ("127.0.0.1", 0),
                timeout=args.timeout,
            )
            uswest = connect_client(
                args.private_host,
                args.uswest_user,
                uswest_password,
                args.timeout,
                sock=channel,
            )
            return cudy, uswest
        except Exception as exc:
            last_error = exc
            if uswest is not None:
                uswest.close()
            if cudy is not None:
                cudy.close()
            if attempt < args.connect_attempts:
                print(f"Private SSH attempt {attempt}/{args.connect_attempts} failed: {exc}", file=sys.stderr)
                time.sleep(min(10, 2 * attempt))
    raise RuntimeError(f"Private SSH through Cudy failed: {last_error}") from last_error


def hardening_args(args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        login_grace_time=args.login_grace_time,
        per_source_max_startups=args.per_source_max_startups,
        max_startups=args.max_startups,
        ignore_ip=args.ignore_ip,
        skip_fail2ban=args.skip_fail2ban,
        fail2ban_maxretry=args.fail2ban_maxretry,
        fail2ban_findtime=args.fail2ban_findtime,
        fail2ban_bantime=args.fail2ban_bantime,
        agent_user=args.agent_user,
        skip_watchdog=args.skip_watchdog,
        watchdog_stale_seconds=args.watchdog_stale_seconds,
        watchdog_interval_seconds=args.watchdog_interval_seconds,
        skip_firewall_guard=args.skip_firewall_guard,
    )


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cudy-host", default="192.168.8.1")
    parser.add_argument("--cudy-user", default="root")
    parser.add_argument("--cudy-password", default="")
    parser.add_argument("--cudy-password-file", type=Path, default=DEFAULT_CUDY_PASSWORD_FILE)
    parser.add_argument(
        "--private-host",
        required=True,
        help="Verified private SSH address configured on uswest; an AWG client peer address is not valid.",
    )
    parser.add_argument("--private-port", type=int, default=22)
    parser.add_argument("--cudy-awg-interface", default="awg2")
    parser.add_argument("--uswest-user", default="root")
    parser.add_argument("--uswest-password", default="")
    parser.add_argument("--uswest-password-file", type=Path, default=DEFAULT_USWEST_PASSWORD_FILE)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--connect-attempts", type=int, default=3)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--login-grace-time", type=int, default=60)
    parser.add_argument("--per-source-max-startups", type=int, default=20)
    parser.add_argument("--max-startups", default="100:30:300")
    parser.add_argument("--ignore-ip", action="append", default=["195.170.35.108", "10.8.1.0/24", "10.77.0.0/24"])
    parser.add_argument("--skip-fail2ban", action="store_true")
    parser.add_argument("--fail2ban-maxretry", type=int, default=5)
    parser.add_argument("--fail2ban-findtime", default="10m")
    parser.add_argument("--fail2ban-bantime", default="1h")
    parser.add_argument(
        "--agent-user",
        action="append",
        default=["cudy-tunnel-windows", "cudy-tunnel-linux", "cudy-tunnel-android"],
    )
    parser.add_argument("--skip-watchdog", action="store_true")
    parser.add_argument("--watchdog-stale-seconds", type=int, default=120)
    parser.add_argument("--watchdog-interval-seconds", type=int, default=60)
    parser.add_argument("--skip-firewall-guard", action="store_true")
    args = parser.parse_args()

    cudy_password = password_value(
        args.cudy_password,
        ("CUDY_SSH_PASSWORD",),
        args.cudy_password_file,
        "Cudy SSH password: ",
    )
    uswest_password = password_value(
        args.uswest_password,
        ("USWEST_SSH_PASSWORD", "CONTROL_BACKUP_SSH_PASSWORD"),
        args.uswest_password_file,
        "uswest root password: ",
    )

    cudy, uswest = connect_via_cudy(args, cudy_password, uswest_password)
    try:
        print(f"Private SSH through Cudy: OK ({args.cudy_host} -> {args.private_host}:{args.private_port})")
        if args.check_only:
            command = "systemctl is-active ssh cudy-sshd-watchdog.timer cudy-ssh-firewall-guard.service; sshd -T | grep -Ei '^(logingracetime|maxstartups|persourcemaxstartups|usedns)'"
            print(harden.ssh_exec(uswest, command, timeout=args.timeout), end="")
            return 0

        remote = "/root/cudy-harden-control-ssh.sh"
        sftp = uswest.open_sftp()
        try:
            with sftp.file(remote, "w") as handle:
                handle.write(harden.remote_script(hardening_args(args)))
            sftp.chmod(remote, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        finally:
            sftp.close()
        print(harden.ssh_exec(uswest, remote, timeout=max(args.timeout, 240)), end="")
    finally:
        uswest.close()
        cudy.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
