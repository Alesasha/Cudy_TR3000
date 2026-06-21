#!/usr/bin/env python3
"""Backup the live control-server through the restricted tunnel SSH user.

This is the backup equivalent of deploy_control_server_via_tunnel_user.py. It
avoids direct root SSH, which can be unreliable when the public sshd is busy at
the pre-auth/banner stage. The tunnel user connects by key, then a short root
script creates a SQLite-consistent archive and hands that temporary archive back
to the tunnel user for download.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
import stat
import time
from pathlib import Path
from typing import Any

import paramiko

from backup_control_server import (
    DEFAULT_HOST,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_REMOTE_DIR,
    prune_backups,
    remote_file_size,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TUNNEL_USER = "cudy-tunnel-windows"
DEFAULT_KEY = ROOT / "secrets" / "agents" / "isasha_R7_Cudy-windows" / "uswest_control_tunnel_ed25519"


def root_password(explicit: str | None) -> str:
    if explicit:
        return explicit
    for name in (
        "CONTROL_BACKUP_ROOT_PASSWORD",
        "USWEST_ROOT_PASSWORD",
        "CONTROL_BACKUP_SSH_PASSWORD",
        "USWEST_SSH_PASSWORD",
    ):
        value = os.environ.get(name)
        if value:
            return value
    return getpass.getpass("Root password for uswest su: ")


def connect(args: argparse.Namespace) -> paramiko.SSHClient:
    key = paramiko.Ed25519Key.from_private_key_file(str(args.key))
    last_error: Exception | None = None
    for attempt in range(1, max(1, args.connect_attempts) + 1):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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
            client.close()
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
            raise TimeoutError(f"su backup script timed out after {timeout}s")
        time.sleep(0.1)
    out = b"".join(out_chunks).decode("utf-8", errors="replace")
    err = b"".join(err_chunks).decode("utf-8", errors="replace")
    rc = channel.recv_exit_status()
    if rc != 0:
        raise RuntimeError(f"su backup script failed rc={rc}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    return out + err


def build_remote_script(args: argparse.Namespace, *, remote_archive: str) -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    remote_tmp = f"/tmp/cudy-control-backup-{timestamp}"
    include_secrets = not args.no_secrets
    include_secrets_flag = "1" if include_secrets else "0"
    return f"""set -eu
REMOTE_DIR={shlex.quote(args.remote_dir)}
TMP={shlex.quote(remote_tmp)}
ARCHIVE={shlex.quote(remote_archive)}
INCLUDE_SECRETS={include_secrets_flag}
TUNNEL_USER={shlex.quote(args.tunnel_user)}
rm -rf "$TMP"
mkdir -p "$TMP/stage/data"
python3 - "$REMOTE_DIR/data/vpn_control.db" "$TMP/stage/data/vpn_control.db" <<'PY'
import sqlite3
import sys
src, dst = sys.argv[1], sys.argv[2]
source = sqlite3.connect(src)
target = sqlite3.connect(dst)
try:
    source.backup(target)
finally:
    target.close()
    source.close()
PY
for item in config deploy docs openwrt tools requirements.txt; do
  if [ -e "$REMOTE_DIR/$item" ]; then
    cp -a "$REMOTE_DIR/$item" "$TMP/stage/$item"
  fi
done
if [ "$INCLUDE_SECRETS" = "1" ] && [ -e "$REMOTE_DIR/secrets" ]; then
  cp -a "$REMOTE_DIR/secrets" "$TMP/stage/secrets"
fi
find "$TMP/stage" -type d -name __pycache__ -exec rm -rf {{}} +
find "$TMP/stage" -type f \\( -name '*.pyc' -o -name '*.pyo' \\) -delete
cat > "$TMP/stage/backup-metadata.txt" <<EOF
source_host={args.host}
remote_dir={args.remote_dir}
created_utc={timestamp}
include_secrets={include_secrets}
sqlite_backup=online
transport=tunnel-user-su
EOF
tar -C "$TMP/stage" -czf "$ARCHIVE" .
chown "$TUNNEL_USER":"$TUNNEL_USER" "$ARCHIVE"
chmod 600 "$ARCHIVE"
rm -rf "$TMP"
ls -lh "$ARCHIVE"
"""


def backup(args: argparse.Namespace) -> dict[str, Any]:
    password = root_password(args.root_password)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    remote_archive = f"/tmp/cudy-control-{args.host.replace('.', '-')}-{timestamp}.tgz"
    local_archive = output_dir / Path(remote_archive).name
    client = connect(args)
    removed: list[Path] = []
    size = 0
    remote_script = f"/tmp/cudy-control-backup-via-tunnel-{int(time.time())}.sh"
    try:
        sftp = client.open_sftp()
        try:
            script = build_remote_script(args, remote_archive=remote_archive)
            mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IROTH
            with sftp.file(remote_script, "w") as remote_fh:
                remote_fh.write(script)
            sftp.chmod(remote_script, mode)
        finally:
            sftp.close()
        run_su_script(client, password=password, script_path=remote_script, timeout=args.timeout * 4)
        sftp = client.open_sftp()
        try:
            sftp.get_channel().settimeout(args.timeout)
            size = remote_file_size(sftp, remote_archive)
            sftp.get(remote_archive, str(local_archive))
            if not args.keep_remote:
                sftp.remove(remote_archive)
            sftp.remove(remote_script)
        finally:
            sftp.close()
        removed = prune_backups(output_dir, keep=args.keep_local)
    finally:
        client.close()
    return {
        "host": args.host,
        "archive": str(local_archive),
        "bytes": size,
        "removed": [str(path) for path in removed],
        "includes_secrets": not args.no_secrets,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backup control-server through cudy-tunnel-windows plus su.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--tunnel-user", default=DEFAULT_TUNNEL_USER)
    parser.add_argument("--key", type=Path, default=DEFAULT_KEY)
    parser.add_argument("--root-password")
    parser.add_argument("--connect-attempts", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--keep-local", type=int, default=10, help="Keep the newest N local backup archives.")
    parser.add_argument("--keep-remote", action="store_true", help="Do not delete the temporary remote archive.")
    parser.add_argument("--no-secrets", action="store_true", help="Exclude remote secrets/ from the archive.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = backup(args)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Backup saved via tunnel user: {result['archive']} ({result['bytes']} bytes)")
    print(f"Includes secrets: {result['includes_secrets']}")
    if result["removed"]:
        print("Pruned old backups:")
        for path in result["removed"]:
            print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
