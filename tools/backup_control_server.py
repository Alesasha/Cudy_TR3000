#!/usr/bin/env python3
"""Create a local disaster-recovery backup of a live Cudy control-server.

The SQLite database is copied with the sqlite online backup API, so the service
does not need to be stopped. The archive also includes control-server code,
config, deploy templates, provider secrets, and docs needed for a fast restore.
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
from typing import Any

import paramiko


DEFAULT_HOST = "95.182.91.203"
DEFAULT_USER = "root"
DEFAULT_REMOTE_DIR = "/opt/cudy-control"
DEFAULT_OUTPUT_DIR = Path("backups") / "control-server"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PASSWORD_FILE = ROOT / "secrets" / "control_backup_ssh_password.txt"
DEFAULT_CUDY_HOST = "192.168.8.1"
DEFAULT_CUDY_PASSWORD_FILE = ROOT / "secrets" / "cudy_ssh_password.txt"
DEFAULT_PRIVATE_HOST = "172.29.172.1"


def ssh_password(explicit: str | None, *, host: str) -> str:
    if explicit:
        return explicit
    for name in ("CONTROL_BACKUP_SSH_PASSWORD", "USWEST_SSH_PASSWORD", "AWG_SSH_PASSWORD_HOSTVDS_USWEST", "AWG_SSH_PASSWORD"):
        value = os.environ.get(name)
        if value:
            return value
    if DEFAULT_PASSWORD_FILE.exists():
        value = DEFAULT_PASSWORD_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    if not sys.stdin.isatty():
        raise RuntimeError(
            "SSH password is required. Set CONTROL_BACKUP_SSH_PASSWORD, "
            f"write {DEFAULT_PASSWORD_FILE}, or pass --ssh-password."
        )
    return getpass.getpass(f"SSH password for {host}: ")


def connect(
    host: str,
    user: str,
    password: str,
    timeout: int,
    *,
    attempts: int,
    sock: object | None = None,
) -> paramiko.SSHClient:
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
                sock=sock,
            )
            return client
        except Exception as exc:
            last_error = exc
            client.close()
            if attempt >= attempts or sock is not None:
                break
            time.sleep(min(20, 2 * attempt))
    raise RuntimeError(f"SSH connect failed after {max(1, attempts)} attempt(s): {last_error}") from last_error


def cudy_password(explicit: str | None, password_file: Path) -> str:
    if explicit:
        return explicit
    value = os.environ.get("CUDY_SSH_PASSWORD", "").strip()
    if value:
        return value
    if password_file.exists():
        value = password_file.read_text(encoding="utf-8-sig").strip()
        if value:
            return value
    if not sys.stdin.isatty():
        raise RuntimeError(f"Cudy SSH password is required in {password_file}")
    return getpass.getpass("Cudy SSH password: ")


def connect_via_cudy(
    args: argparse.Namespace,
    password: str,
) -> tuple[paramiko.SSHClient, paramiko.SSHClient]:
    router = connect(
        args.cudy_host,
        args.cudy_user,
        cudy_password(args.cudy_password, args.cudy_password_file),
        args.timeout,
        attempts=args.connect_attempts,
    )
    try:
        ssh_exec(
            router,
            f"ip -4 route replace {shlex.quote(args.private_host)}/32 dev {shlex.quote(args.cudy_awg_interface)}",
            args.timeout,
        )
        transport = router.get_transport()
        if transport is None or not transport.is_active():
            raise RuntimeError("Cudy SSH transport is not active")
        channel = transport.open_channel(
            "direct-tcpip",
            (args.private_host, args.private_port),
            ("127.0.0.1", 0),
            timeout=args.timeout,
        )
        try:
            target = connect(
                args.private_host,
                args.user,
                password,
                args.timeout,
                attempts=1,
                sock=channel,
            )
        except Exception:
            channel.close()
            raise
        return router, target
    except Exception:
        router.close()
        raise


def ssh_exec(client: paramiko.SSHClient, command: str, timeout: int) -> str:
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
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
            raise TimeoutError(f"remote command timed out after {timeout}s: {command}")
        time.sleep(0.1)
    out = b"".join(out_chunks).decode("utf-8", errors="replace")
    err = b"".join(err_chunks).decode("utf-8", errors="replace")
    rc = channel.recv_exit_status()
    if rc != 0:
        raise RuntimeError(f"remote command failed rc={rc}: {command}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    return out + err


def remote_file_size(sftp: paramiko.SFTPClient, path: str) -> int:
    attrs = sftp.stat(path)
    mode = attrs.st_mode
    if not stat.S_ISREG(mode):
        raise RuntimeError(f"remote path is not a regular file: {path}")
    return int(attrs.st_size)


def prune_backups(output_dir: Path, *, keep: int) -> list[Path]:
    if keep <= 0:
        return []
    archives = sorted(output_dir.glob("cudy-control-*.tgz"), key=lambda item: item.stat().st_mtime, reverse=True)
    removed: list[Path] = []
    for path in archives[keep:]:
        path.unlink()
        removed.append(path)
    return removed


def create_remote_backup(
    client: paramiko.SSHClient,
    *,
    host: str,
    remote_dir: str,
    include_secrets: bool,
    timeout: int,
) -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    remote_tmp = f"/tmp/cudy-control-backup-{timestamp}"
    remote_archive = f"/tmp/cudy-control-{host.replace('.', '-')}-{timestamp}.tgz"
    include_secrets_flag = "1" if include_secrets else "0"
    command = f"""set -eu
REMOTE_DIR={shlex.quote(remote_dir)}
TMP={shlex.quote(remote_tmp)}
ARCHIVE={shlex.quote(remote_archive)}
INCLUDE_SECRETS={include_secrets_flag}
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
source_host={host}
remote_dir={remote_dir}
created_utc={timestamp}
include_secrets={include_secrets}
sqlite_backup=online
EOF
tar -C "$TMP/stage" -czf "$ARCHIVE" .
rm -rf "$TMP"
ls -lh "$ARCHIVE"
"""
    ssh_exec(client, command, timeout)
    return remote_archive


def backup(args: argparse.Namespace) -> dict[str, Any]:
    password = ssh_password(args.ssh_password, host=args.host)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    router_client: paramiko.SSHClient | None = None
    if args.via_cudy:
        router_client, client = connect_via_cudy(args, password)
    else:
        client = connect(args.host, args.user, password, args.timeout, attempts=args.connect_attempts)
    remote_archive = ""
    local_archive = output_dir / "not-created.tgz"
    size = 0
    removed: list[Path] = []
    try:
        remote_archive = create_remote_backup(
            client,
            host=args.host,
            remote_dir=args.remote_dir,
            include_secrets=not args.no_secrets,
            timeout=args.timeout * 3,
        )
        local_archive = output_dir / Path(remote_archive).name
        sftp = client.open_sftp()
        try:
            sftp.get_channel().settimeout(args.timeout)
            size = remote_file_size(sftp, remote_archive)
            sftp.get(remote_archive, str(local_archive))
        finally:
            sftp.close()
        if not args.keep_remote:
            ssh_exec(client, f"rm -f {shlex.quote(remote_archive)}", args.timeout)
        removed = prune_backups(output_dir, keep=args.keep_local)
    finally:
        if remote_archive and not args.keep_remote:
            try:
                ssh_exec(client, f"rm -f {shlex.quote(remote_archive)}", args.timeout)
            except Exception:
                pass
        client.close()
        if router_client is not None:
            router_client.close()
    return {
        "host": args.host,
        "archive": str(local_archive),
        "bytes": size,
        "removed": [str(path) for path in removed],
        "includes_secrets": not args.no_secrets,
        "mode": "ssh_via_cudy" if args.via_cudy else "ssh_direct",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backup Cudy control-server from a live remote host.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--ssh-password")
    parser.add_argument("--connect-attempts", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    parser.add_argument("--via-cudy", action="store_true", help="Use the private uswest SSH path through Cudy.")
    parser.add_argument("--cudy-host", default=DEFAULT_CUDY_HOST)
    parser.add_argument("--cudy-user", default="root")
    parser.add_argument("--cudy-password")
    parser.add_argument("--cudy-password-file", type=Path, default=DEFAULT_CUDY_PASSWORD_FILE)
    parser.add_argument("--private-host", default=DEFAULT_PRIVATE_HOST)
    parser.add_argument("--private-port", type=int, default=22)
    parser.add_argument("--cudy-awg-interface", default="awg2")
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
    print(f"Backup saved: {result['archive']} ({result['bytes']} bytes)")
    print(f"Includes secrets: {result['includes_secrets']}")
    print(f"Mode: {result['mode']}")
    if result["removed"]:
        print("Pruned old backups:")
        for path in result["removed"]:
            print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
