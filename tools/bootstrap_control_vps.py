#!/usr/bin/env python3
"""Bootstrap a fresh Ubuntu/Debian VPS for Cudy control-server recovery.

The provider still has to create the VPS and install Ubuntu before SSH exists.
This script starts after root SSH works. It installs the boring base packages,
Docker runtime for Amnezia/exit experiments, creates the control-server system
user, and prints a readiness report. It does not overwrite or install Amnezia
server configuration.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
from dataclasses import dataclass

import paramiko


DEFAULT_USER = "root"
DEFAULT_REMOTE_DIR = "/opt/cudy-control"
DEFAULT_SERVICE_USER = "cudy-control"


@dataclass(frozen=True)
class Step:
    name: str
    command: str


def password_from_env_or_prompt(explicit: str | None, *, host: str) -> str:
    if explicit:
        return explicit
    for name in ("TARGET_SSH_PASSWORD", "BOOTSTRAP_SSH_PASSWORD", "USWEST_SSH_PASSWORD", "AWG_SSH_PASSWORD"):
        value = os.environ.get(name)
        if value:
            return value
    return getpass.getpass(f"SSH password for {host}: ")


def connect(host: str, user: str, password: str, timeout: int) -> paramiko.SSHClient:
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
    )
    return client


def ssh_exec(client: paramiko.SSHClient, command: str, timeout: int) -> str:
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        raise RuntimeError(f"remote command failed rc={rc}: {command}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    return out + err


def build_steps(args: argparse.Namespace) -> list[Step]:
    packages = [
        "ca-certificates",
        "curl",
        "docker-compose-plugin",
        "docker.io",
        "iptables",
        "iproute2",
        "jq",
        "python3",
        "python3-paramiko",
        "python3-pip",
        "python3-venv",
        "sqlite3",
        "tar",
        "ufw",
    ]
    steps = [
        Step(
            "verify apt host",
            "set -eu\n"
            "test -f /etc/os-release\n"
            ". /etc/os-release\n"
            'case "${ID:-}:${ID_LIKE:-}" in *ubuntu*|*debian*) true ;; *) echo "Unsupported OS: ${PRETTY_NAME:-unknown}" >&2; exit 2 ;; esac\n',
        ),
        Step(
            "apt install base packages",
            "set -eu\n"
            "export DEBIAN_FRONTEND=noninteractive\n"
            "apt-get update -y\n"
            f"apt-get install -y {' '.join(shlex.quote(pkg) for pkg in packages)}\n",
        ),
        Step(
            "enable docker",
            "set -eu\n"
            "systemctl enable --now docker\n"
            "docker version >/dev/null\n",
        ),
        Step(
            "create control user and directories",
            "set -eu\n"
            f"id -u {shlex.quote(args.service_user)} >/dev/null 2>&1 || "
            f"useradd --system --home {shlex.quote(args.remote_dir)} --shell /usr/sbin/nologin {shlex.quote(args.service_user)}\n"
            f"mkdir -p {shlex.quote(args.remote_dir)}\n"
            f"chown {shlex.quote(args.service_user)}:{shlex.quote(args.service_user)} {shlex.quote(args.remote_dir)}\n",
        ),
    ]
    if args.hostname:
        steps.insert(
            1,
            Step(
                "set hostname",
                f"set -eu\nhostnamectl set-hostname {shlex.quote(args.hostname)}\n",
            ),
        )
    return steps


def readiness_report(client: paramiko.SSHClient, timeout: int) -> str:
    command = r"""set -eu
echo "== os =="
cat /etc/os-release | sed -n 's/^\(PRETTY_NAME\|VERSION_ID\)=//p'
echo
echo "== python =="
python3 --version
echo
echo "== docker =="
docker --version
docker compose version || true
echo
echo "== service user =="
id cudy-control || true
echo
echo "== listening ports =="
ss -lntup || true
"""
    return ssh_exec(client, command, timeout)


def run(args: argparse.Namespace) -> str:
    steps = build_steps(args)
    if args.dry_run:
        lines = ["Dry run. Remote commands:"]
        for step in steps:
            lines.append(f"\n## {step.name}\n{step.command.rstrip()}")
        return "\n".join(lines)

    password = password_from_env_or_prompt(args.ssh_password, host=args.host)
    client = connect(args.host, args.user, password, args.timeout)
    try:
        output: list[str] = []
        for step in steps:
            output.append(f"== {step.name} ==")
            output.append(ssh_exec(client, step.command, args.timeout * 8).strip())
        output.append("== readiness ==")
        output.append(readiness_report(client, args.timeout).strip())
        return "\n".join(item for item in output if item)
    finally:
        client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap a fresh Ubuntu/Debian VPS for Cudy control-server recovery.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--ssh-password")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--hostname")
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    parser.add_argument("--service-user", default=DEFAULT_SERVICE_USER)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        print(run(args))
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
