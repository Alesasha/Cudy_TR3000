#!/usr/bin/env python3
"""Deploy the Python control-server MVP to uswest.

The service is intentionally bound to 127.0.0.1. Operators and early agents
reach it through SSH local port forwarding.
"""

from __future__ import annotations

import argparse
import getpass
import os
import posixpath
import shlex
import stat
import sys
import time
from pathlib import Path

import paramiko


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "95.182.91.203"
DEFAULT_USER = "root"
DEFAULT_REMOTE_DIR = "/opt/cudy-control"
DEFAULT_SERVICE = "vpn-control"

UPLOAD_DIRS = ["config", "deploy", "docs", "openwrt", "tools"]
UPLOAD_FILES = ["requirements.txt"]
EXCLUDE_NAMES = {
    ".git",
    "__pycache__",
    "node_modules",
    ".playwright-cli",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp", ".bak"}


def ssh_password(explicit: str | None) -> str:
    if explicit:
        return explicit
    for name in ("USWEST_SSH_PASSWORD", "AWG_SSH_PASSWORD_HOSTVDS_USWEST", "AWG_SSH_PASSWORD"):
        value = os.environ.get(name)
        if value:
            return value
    return getpass.getpass("SSH password for uswest: ")


def connect(host: str, user: str, password: str, timeout: int, *, attempts: int) -> paramiko.SSHClient:
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
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
        except Exception as exc:
            last_error = exc
            client.close()
            if attempt >= attempts:
                break
            time.sleep(min(20, 2 * attempt))
    raise RuntimeError(f"SSH connect failed after {max(1, attempts)} attempt(s): {last_error}") from last_error


def ssh_exec(client: paramiko.SSHClient, command: str, timeout: int) -> str:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        raise RuntimeError(f"remote command failed rc={rc}: {command}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    return out + err


def mkdir_p(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    parts = [part for part in remote_dir.split("/") if part]
    path = ""
    for part in parts:
        path += "/" + part
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)


def should_skip(path: Path) -> bool:
    if path.name in EXCLUDE_NAMES:
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    return False


def upload_file(sftp: paramiko.SFTPClient, local: Path, remote: str) -> None:
    mkdir_p(sftp, posixpath.dirname(remote))
    sftp.put(str(local), remote)


def upload_tree(sftp: paramiko.SFTPClient, local_dir: Path, remote_dir: str) -> int:
    count = 0
    for path in local_dir.rglob("*"):
        if any(should_skip(parent) for parent in path.relative_to(local_dir).parents if str(parent) != "."):
            continue
        if should_skip(path):
            continue
        rel = path.relative_to(local_dir).as_posix()
        remote = posixpath.join(remote_dir, rel)
        if path.is_dir():
            mkdir_p(sftp, remote)
        elif path.is_file():
            upload_file(sftp, path, remote)
            count += 1
    return count


def remote_file_exists(sftp: paramiko.SFTPClient, path: str) -> bool:
    try:
        mode = sftp.stat(path).st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISREG(mode)


def deploy(args: argparse.Namespace) -> dict[str, object]:
    password = ssh_password(args.ssh_password)
    client = connect(args.host, args.user, password, args.timeout, attempts=args.connect_attempts)
    uploaded = 0
    try:
        package_step = ""
        if not args.skip_package_install:
            package_step = (
                "if command -v apt-get >/dev/null 2>&1; then\n"
                "  if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import paramiko' >/dev/null 2>&1; then\n"
                "    export DEBIAN_FRONTEND=noninteractive\n"
                "    apt-get update -y\n"
                "    apt-get install -y python3 python3-paramiko curl tar\n"
                "  fi\n"
                "fi\n"
            )
        ssh_exec(
            client,
            "set -eu\n"
            f"{package_step}"
            f"id -u {shlex.quote(args.service_user)} >/dev/null 2>&1 || "
            f"useradd --system --home {shlex.quote(args.remote_dir)} --shell /usr/sbin/nologin {shlex.quote(args.service_user)}\n"
            f"mkdir -p {shlex.quote(args.remote_dir)} {shlex.quote(args.remote_dir + '/data')}\n",
            args.timeout * 6,
        )
        sftp = client.open_sftp()
        try:
            for dirname in UPLOAD_DIRS:
                local_dir = ROOT / dirname
                if local_dir.exists():
                    uploaded += upload_tree(sftp, local_dir, posixpath.join(args.remote_dir, dirname))
            for filename in UPLOAD_FILES:
                local_file = ROOT / filename
                if local_file.exists():
                    upload_file(sftp, local_file, posixpath.join(args.remote_dir, filename))
                    uploaded += 1
            local_db = args.db
            remote_db = posixpath.join(args.remote_dir, "data", "vpn_control.db")
            if args.upload_db and local_db.exists():
                if remote_file_exists(sftp, remote_db):
                    backup = f"{remote_db}.bak-{int(__import__('time').time())}"
                    ssh_exec(client, f"cp {shlex.quote(remote_db)} {shlex.quote(backup)}", args.timeout)
                upload_file(sftp, local_db, remote_db)
                uploaded += 1
        finally:
            sftp.close()

        ssh_exec(
            client,
            "set -eu\n"
            f"chown -R {shlex.quote(args.service_user)}:{shlex.quote(args.service_user)} {shlex.quote(args.remote_dir)}\n"
            f"cp {shlex.quote(args.remote_dir + '/deploy/uswest/vpn-control.service')} /etc/systemd/system/{shlex.quote(args.service_name)}.service\n"
            "systemctl daemon-reload\n"
            f"systemctl enable --now {shlex.quote(args.service_name)}\n"
            f"systemctl restart {shlex.quote(args.service_name)}\n"
            "for i in $(seq 1 30); do "
            "curl -fsS http://127.0.0.1:8765/healthz >/tmp/vpn-control-health.json 2>/tmp/vpn-control-health.err && break; "
            "sleep 1; "
            "done\n"
            f"systemctl --no-pager --full status {shlex.quote(args.service_name)} | head -40\n"
            "cat /tmp/vpn-control-health.json\n",
            args.timeout * 3,
        )
    finally:
        client.close()
    return {"host": args.host, "remote_dir": args.remote_dir, "uploaded_files": uploaded}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deploy control-server MVP to uswest.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--ssh-password")
    parser.add_argument("--connect-attempts", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    parser.add_argument("--service-name", default=DEFAULT_SERVICE)
    parser.add_argument("--service-user", default="cudy-control")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "vpn_control.db")
    parser.add_argument("--skip-package-install", action="store_true", help="Skip apt/package checks on an already prepared VPS.")
    parser.add_argument("--no-upload-db", dest="upload_db", action="store_false")
    parser.set_defaults(upload_db=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = deploy(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Deployed to {result['host']}:{result['remote_dir']} ({result['uploaded_files']} files)")
    print("Open an SSH tunnel: ssh -N -L 8765:127.0.0.1:8765 root@95.182.91.203")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
