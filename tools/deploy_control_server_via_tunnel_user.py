#!/usr/bin/env python3
"""Deploy control-server through the restricted tunnel SSH user.

This is a fallback for moments when direct root SSH is unreliable because the
public sshd is busy at the pre-auth/banner stage. The tunnel user normally keeps
working because agents already use it for local port forwarding.
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import stat
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Iterable

import paramiko


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "95.182.91.203"
DEFAULT_TUNNEL_USER = "cudy-tunnel-windows"
DEFAULT_KEY = ROOT / "secrets" / "agents" / "isasha_R7_Cudy-windows" / "uswest_control_tunnel_ed25519"
DEFAULT_REMOTE_DIR = "/opt/cudy-control"
DEFAULT_SERVICE = "vpn-control"
DEFAULT_PASSWORD_FILE = ROOT / "secrets" / "control_backup_ssh_password.txt"
UPLOAD_DIRS = ["config", "deploy", "docs", "openwrt", "tools"]
AGENT_UPDATE_DIR = "build/agent-updates"
AGENT_ENROLLMENT_DIR = "build/universal-agents"
UPLOAD_FILES = ["requirements.txt"]
EXCLUDE_NAMES = {".git", "__pycache__", "node_modules", ".playwright-cli"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp", ".bak"}


def root_password(explicit: str | None) -> str:
    if explicit:
        return explicit
    for name in ("USWEST_ROOT_PASSWORD", "USWEST_SSH_PASSWORD", "CONTROL_DEPLOY_SSH_PASSWORD"):
        value = os.environ.get(name)
        if value:
            return value
    if DEFAULT_PASSWORD_FILE.exists():
        value = DEFAULT_PASSWORD_FILE.read_text(encoding="utf-8-sig").strip()
        if value:
            return value
    return getpass.getpass("Root password for uswest su: ")


def should_skip(path: Path) -> bool:
    return path.name in EXCLUDE_NAMES or path.suffix in EXCLUDE_SUFFIXES


def archive_paths(*, include_agent_updates: bool = True) -> Iterable[Path]:
    directories = [
        *UPLOAD_DIRS,
        *([AGENT_UPDATE_DIR, AGENT_ENROLLMENT_DIR] if include_agent_updates else []),
    ]
    for dirname in directories:
        path = ROOT / dirname
        if path.exists():
            yield path
    for filename in UPLOAD_FILES:
        path = ROOT / filename
        if path.exists():
            yield path


def build_archive(output: Path, *, include_agent_updates: bool = True) -> int:
    count = 0
    with tarfile.open(output, "w") as tar:
        for base in archive_paths(include_agent_updates=include_agent_updates):
            if base.is_file():
                tar.add(base, arcname=base.relative_to(ROOT).as_posix())
                count += 1
                continue
            for path in base.rglob("*"):
                rel = path.relative_to(ROOT)
                if any(should_skip(parent) for parent in rel.parents if str(parent) != "."):
                    continue
                if should_skip(path):
                    continue
                tar.add(path, arcname=rel.as_posix())
                if path.is_file():
                    count += 1
    return count


def connect(args: argparse.Namespace) -> paramiko.SSHClient:
    key = paramiko.Ed25519Key.from_private_key_file(str(args.key))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    last_error: Exception | None = None
    for attempt in range(1, max(1, args.connect_attempts) + 1):
        try:
            client.connect(
                args.host,
                username=args.tunnel_user,
                pkey=key,
                timeout=args.timeout,
                banner_timeout=args.timeout,
                auth_timeout=args.timeout,
                look_for_keys=False,
                allow_agent=False,
            )
            return client
        except Exception as exc:
            last_error = exc
            if attempt >= args.connect_attempts:
                break
            time.sleep(min(15, 2 * attempt))
    raise RuntimeError(f"SSH tunnel-user connect failed: {last_error}") from last_error


def run_su_script(client: paramiko.SSHClient, *, password: str, script_path: str, timeout: int) -> str:
    command = f"su -c 'bash {script_path}' root"
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    stdin.write(password + "\n")
    stdin.flush()
    channel = stdout.channel
    channel.settimeout(1.0)
    deadline = time.monotonic() + max(1, timeout)
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    while True:
        while channel.recv_ready():
            out_chunks.append(channel.recv(65536))
        while channel.recv_stderr_ready():
            err_chunks.append(channel.recv_stderr(65536))
        if channel.exit_status_ready():
            break
        if time.monotonic() >= deadline:
            channel.close()
            raise TimeoutError(f"su deploy script timed out after {timeout}s")
        time.sleep(0.1)
    out = b"".join(out_chunks).decode("utf-8", errors="replace")
    err = b"".join(err_chunks).decode("utf-8", errors="replace")
    rc = channel.recv_exit_status()
    if rc != 0:
        raise RuntimeError(f"su deploy script failed rc={rc}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    return out + err


def promotion_script(args: argparse.Namespace, *, remote_archive: str, remote_script: str) -> str:
    return f"""set -eu
if ! python3 -c 'import paramiko' >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y python3 python3-paramiko openssh-client curl tar
fi
mkdir -p {args.remote_dir} {args.remote_dir}/data
cd {args.remote_dir}
tar -xf {remote_archive}
chown -R {args.service_user}:{args.service_user} {args.remote_dir}
python3 {args.remote_dir}/tools/install_agent_provisioning_ssh.py --service-user {args.service_user} --keys-path {args.remote_dir}/data/agent_authorized_keys
cp {args.remote_dir}/deploy/uswest/vpn-control.service /etc/systemd/system/{args.service_name}.service
cp {args.remote_dir}/deploy/uswest/vpn-control-provider-refresh.service /etc/systemd/system/vpn-control-provider-refresh.service
cp {args.remote_dir}/deploy/uswest/vpn-control-provider-refresh.timer /etc/systemd/system/vpn-control-provider-refresh.timer
systemctl daemon-reload
systemctl enable --now {args.service_name} >/dev/null
systemctl enable --now vpn-control-provider-refresh.timer >/dev/null
systemctl restart {args.service_name}
for i in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8765/healthz 2>/tmp/vpn-control-health.err && break
  sleep 1
done
python3 {args.remote_dir}/tools/vpn_control_app.py --db {args.remote_dir}/data/vpn_control.db --inventory {args.remote_dir}/config/vpn_inventory.json system-status
rm -f {remote_archive} {remote_script}
"""


def openssh_options(args: argparse.Namespace) -> list[str]:
    return [
        "-i",
        str(args.key),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={args.timeout}",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
    ]


def run_openssh(
    command: list[str],
    *,
    attempts: int,
    timeout: int,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    last_result: subprocess.CompletedProcess[str] | None = None
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            result = subprocess.run(
                command,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            last_result = result
            if result.returncode == 0:
                return result
            last_error = RuntimeError(result.stderr.strip() or result.stdout.strip() or f"rc={result.returncode}")
        except subprocess.TimeoutExpired as exc:
            last_error = exc
        if attempt < attempts:
            print(f"OpenSSH attempt {attempt}/{attempts} failed: {last_error}", file=sys.stderr, flush=True)
            time.sleep(min(15, 2 * attempt))
    if last_result is not None:
        raise RuntimeError(
            f"OpenSSH command failed rc={last_result.returncode}\nSTDOUT:\n{last_result.stdout}\nSTDERR:\n{last_result.stderr}"
        )
    raise RuntimeError(f"OpenSSH command failed: {last_error}") from last_error


def deploy_openssh(args: argparse.Namespace, *, archive: Path, password: str, count: int, temp_dir: Path) -> dict[str, object]:
    timestamp = int(time.time())
    remote_archive = f"/tmp/cudy-control-deploy-{timestamp}.tar"
    remote_script = f"/tmp/cudy-control-promote-{timestamp}.sh"
    local_script = temp_dir / "cudy-control-promote.sh"
    local_script.write_text(
        promotion_script(args, remote_archive=remote_archive, remote_script=remote_script),
        encoding="utf-8",
        newline="\n",
    )
    destination = f"{args.tunnel_user}@{args.host}"
    options = openssh_options(args)
    print(f"Uploading archive to {remote_archive} with system OpenSSH...", flush=True)
    run_openssh(
        ["scp", *options, str(archive), f"{destination}:{remote_archive}"],
        attempts=args.connect_attempts,
        timeout=max(args.timeout * 4, 120),
    )
    run_openssh(
        ["scp", *options, str(local_script), f"{destination}:{remote_script}"],
        attempts=args.connect_attempts,
        timeout=max(args.timeout * 3, 90),
    )
    print("Promoting archive as root and restarting service with system OpenSSH...", flush=True)
    su_command = f"script -qec \"su -c 'bash {remote_script}' root\" /dev/null"
    result = run_openssh(
        ["ssh", *options, destination, su_command],
        attempts=args.connect_attempts,
        timeout=max(args.timeout * 8, 300),
        input_text=password + "\n",
    )
    print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr, flush=True)
    return {"host": args.host, "remote_dir": args.remote_dir, "uploaded_files": count}


def deploy(args: argparse.Namespace) -> dict[str, object]:
    password = root_password(args.root_password)
    with tempfile.TemporaryDirectory(prefix="cudy-control-deploy-") as temp_dir:
        archive = Path(temp_dir) / "cudy-control-deploy.tar"
        count = build_archive(archive, include_agent_updates=not args.skip_agent_updates)
        print(f"Built archive: {archive} ({archive.stat().st_size} bytes, {count} files)", flush=True)
        if args.openssh:
            return deploy_openssh(args, archive=archive, password=password, count=count, temp_dir=Path(temp_dir))
        client = connect(args)
        try:
            remote_archive = f"/tmp/cudy-control-deploy-{int(time.time())}.tar"
            remote_script = f"/tmp/cudy-control-promote-{int(time.time())}.sh"
            sftp = client.open_sftp()
            sftp.get_channel().settimeout(args.timeout)
            try:
                print(f"Uploading archive to {remote_archive}...", flush=True)
                sftp.put(str(archive), remote_archive)
                script = promotion_script(args, remote_archive=remote_archive, remote_script=remote_script)
                info = paramiko.SFTPAttributes()
                info.st_mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IROTH
                with sftp.file(remote_script, "w") as remote_fh:
                    remote_fh.write(script)
                sftp.chmod(remote_script, info.st_mode)
            finally:
                sftp.close()
            print("Promoting archive as root and restarting service...", flush=True)
            output = run_su_script(client, password=password, script_path=remote_script, timeout=args.timeout * 4)
            print(output, end="" if output.endswith("\n") else "\n", flush=True)
            return {"host": args.host, "remote_dir": args.remote_dir, "uploaded_files": count}
        finally:
            client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deploy control-server through cudy-tunnel-windows plus su.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--tunnel-user", default=DEFAULT_TUNNEL_USER)
    parser.add_argument("--key", type=Path, default=DEFAULT_KEY)
    parser.add_argument("--root-password")
    parser.add_argument("--connect-attempts", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--skip-agent-updates", action="store_true", help="Deploy code without re-uploading agent release artifacts.")
    parser.add_argument("--openssh", action="store_true", help="Use system ssh/scp instead of Paramiko for banner-sensitive deployments.")
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    parser.add_argument("--service-name", default=DEFAULT_SERVICE)
    parser.add_argument("--service-user", default="cudy-control")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = deploy(args)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Deployed to {result['host']}:{result['remote_dir']} ({result['uploaded_files']} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
