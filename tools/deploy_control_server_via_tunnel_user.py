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
import stat
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
UPLOAD_DIRS = ["config", "deploy", "docs", "openwrt", "tools", "build/agent-updates"]
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
    return getpass.getpass("Root password for uswest su: ")


def should_skip(path: Path) -> bool:
    return path.name in EXCLUDE_NAMES or path.suffix in EXCLUDE_SUFFIXES


def archive_paths() -> Iterable[Path]:
    for dirname in UPLOAD_DIRS:
        path = ROOT / dirname
        if path.exists():
            yield path
    for filename in UPLOAD_FILES:
        path = ROOT / filename
        if path.exists():
            yield path


def build_archive(output: Path) -> int:
    count = 0
    with tarfile.open(output, "w") as tar:
        for base in archive_paths():
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


def deploy(args: argparse.Namespace) -> dict[str, object]:
    password = root_password(args.root_password)
    with tempfile.TemporaryDirectory(prefix="cudy-control-deploy-") as temp_dir:
        archive = Path(temp_dir) / "cudy-control-deploy.tar"
        count = build_archive(archive)
        print(f"Built archive: {archive} ({archive.stat().st_size} bytes, {count} files)", flush=True)
        client = connect(args)
        try:
            remote_archive = f"/tmp/cudy-control-deploy-{int(time.time())}.tar"
            remote_script = f"/tmp/cudy-control-promote-{int(time.time())}.sh"
            sftp = client.open_sftp()
            sftp.get_channel().settimeout(args.timeout)
            try:
                print(f"Uploading archive to {remote_archive}...", flush=True)
                sftp.put(str(archive), remote_archive)
                script = f"""set -eu
mkdir -p {args.remote_dir} {args.remote_dir}/data
cd {args.remote_dir}
tar -xf {remote_archive}
chown -R {args.service_user}:{args.service_user} {args.remote_dir}
cp {args.remote_dir}/deploy/uswest/vpn-control.service /etc/systemd/system/{args.service_name}.service
systemctl daemon-reload
systemctl enable --now {args.service_name} >/dev/null
systemctl restart {args.service_name}
for i in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8765/healthz 2>/tmp/vpn-control-health.err && break
  sleep 1
done
python3 {args.remote_dir}/tools/vpn_control_app.py --db {args.remote_dir}/data/vpn_control.db --inventory {args.remote_dir}/config/vpn_inventory.json system-status
rm -f {remote_archive} {remote_script}
"""
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
