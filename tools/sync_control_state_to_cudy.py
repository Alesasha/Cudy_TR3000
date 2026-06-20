#!/usr/bin/env python3
"""Replicate primary control-server state to Cudy fallback storage.

The secret backup archive is stored under /root on Cudy and is not served by
uhttpd. A small public status JSON is written under /www/cudy-control so agents
and operators can see whether the fallback copy is fresh.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any

import paramiko

from backup_control_server import (
    DEFAULT_HOST as DEFAULT_SOURCE_HOST,
    DEFAULT_REMOTE_DIR as DEFAULT_SOURCE_REMOTE_DIR,
    DEFAULT_USER as DEFAULT_SOURCE_USER,
    connect,
    create_remote_backup,
    remote_file_size,
    ssh_exec,
)
from sync_control_manifest_to_cudy import (
    DEFAULT_CUDY_HOST,
    DEFAULT_CUDY_PASSWORD_FILE,
    DEFAULT_CUDY_USER,
    ssh_write_file,
)
from vpn_control_app import control_endpoints_manifest, now


DEFAULT_CUDY_STATE_DIR = "/root/cudy-control-fallback"
DEFAULT_CUDY_WEB_DIR = "/www/cudy-control"


def source_password(explicit: str | None, *, host: str) -> str:
    if explicit:
        return explicit
    for name in ("CONTROL_BACKUP_SSH_PASSWORD", "USWEST_SSH_PASSWORD", "AWG_SSH_PASSWORD_HOSTVDS_USWEST", "AWG_SSH_PASSWORD"):
        value = os.environ.get(name)
        if value:
            return value
    return getpass.getpass(f"SSH password for source {host}: ")


def cudy_password(explicit: str | None, *, host: str) -> str:
    if explicit:
        return explicit
    for name in ("CUDY_SSH_PASSWORD", "AWG_SSH_PASSWORD_CUDY_HOME", "AWG_SSH_PASSWORD"):
        value = os.environ.get(name)
        if value:
            return value
    if DEFAULT_CUDY_PASSWORD_FILE.exists():
        value = DEFAULT_CUDY_PASSWORD_FILE.read_text(encoding="utf-8-sig").strip()
        if value:
            return value
    return getpass.getpass(f"SSH password for Cudy {host}: ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ssh_write_bytes(client: paramiko.SSHClient, path: str, content: bytes, timeout: int) -> None:
    command = f"cat > {shlex.quote(path)}"
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    stdin.channel.sendall(content)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        raise RuntimeError(f"remote binary write failed rc={rc}: {path}\nSTDOUT:\n{out}\nSTDERR:\n{err}")


def download_source_archive(
    client: paramiko.SSHClient,
    *,
    source_host: str,
    source_remote_dir: str,
    include_secrets: bool,
    timeout: int,
    output_path: Path,
) -> str:
    remote_archive = create_remote_backup(
        client,
        host=source_host,
        remote_dir=source_remote_dir,
        include_secrets=include_secrets,
        timeout=timeout * 3,
    )
    try:
        sftp = client.open_sftp()
        try:
            remote_file_size(sftp, remote_archive)
            sftp.get(remote_archive, str(output_path))
        finally:
            sftp.close()
    finally:
        try:
            ssh_exec(client, f"rm -f {shlex.quote(remote_archive)}", timeout)
        except Exception:
            pass
    return remote_archive


def publish_to_cudy(
    client: paramiko.SSHClient,
    *,
    archive_path: Path,
    source_host: str,
    state_dir: str,
    web_dir: str,
    include_secrets: bool,
    timeout: int,
    keep_remote: int,
) -> dict[str, Any]:
    state_dir = state_dir.rstrip("/")
    web_dir = web_dir.rstrip("/")
    archive_name = archive_path.name
    remote_archive = f"{state_dir}/{archive_name}"
    remote_current = f"{state_dir}/control-state-current.tgz"
    digest = sha256_file(archive_path)
    size = archive_path.stat().st_size
    created_at = now()
    ssh_exec(client, f"mkdir -p {shlex.quote(state_dir)} {shlex.quote(web_dir)}", timeout)
    ssh_write_bytes(client, remote_archive, archive_path.read_bytes(), timeout * 3)
    ssh_exec(
        client,
        "set -eu\n"
        f"chmod 0600 {shlex.quote(remote_archive)}\n"
        f"ln -sf {shlex.quote(archive_name)} {shlex.quote(remote_current)}\n"
        f"cd {shlex.quote(state_dir)}\n"
        f"ls -1t cudy-control-*.tgz 2>/dev/null | tail -n +{int(keep_remote) + 1} | xargs -r rm -f\n",
        timeout,
    )
    status = {
        "schema_version": 1,
        "source_host": source_host,
        "created_at": created_at,
        "include_secrets": include_secrets,
        "archive_name": archive_name,
        "remote_archive": remote_archive,
        "remote_current": remote_current,
        "bytes": size,
        "sha256": digest,
        "endpoint_manifest": control_endpoints_manifest(),
    }
    status_json = json.dumps(status, ensure_ascii=False, indent=2) + "\n"
    ssh_write_file(client, f"{web_dir}/state.json", status_json, timeout)
    ssh_write_file(client, f"{web_dir}/endpoints.json", json.dumps(control_endpoints_manifest(), ensure_ascii=False, indent=2) + "\n", timeout)
    ssh_exec(client, f"chmod 0644 {shlex.quote(web_dir)}/state.json {shlex.quote(web_dir)}/endpoints.json", timeout)
    return status


def sync(args: argparse.Namespace) -> dict[str, Any]:
    src_password = source_password(args.source_password, host=args.source_host)
    dst_password = cudy_password(args.cudy_password, host=args.cudy_host)
    temp_dir = Path(tempfile.mkdtemp(prefix="cudy-control-state-"))
    local_archive = temp_dir / f"cudy-control-{args.source_host.replace('.', '-')}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.tgz"
    source_client: paramiko.SSHClient | None = None
    cudy_client: paramiko.SSHClient | None = None
    try:
        source_client = connect(args.source_host, args.source_user, src_password, args.timeout)
        download_source_archive(
            source_client,
            source_host=args.source_host,
            source_remote_dir=args.source_remote_dir,
            include_secrets=not args.no_secrets,
            timeout=args.timeout,
            output_path=local_archive,
        )
        cudy_client = connect(args.cudy_host, args.cudy_user, dst_password, args.timeout)
        status = publish_to_cudy(
            cudy_client,
            archive_path=local_archive,
            source_host=args.source_host,
            state_dir=args.cudy_state_dir,
            web_dir=args.cudy_web_dir,
            include_secrets=not args.no_secrets,
            timeout=args.timeout,
            keep_remote=args.keep_remote,
        )
        return status
    finally:
        if source_client is not None:
            source_client.close()
        if cudy_client is not None:
            cudy_client.close()
        if not args.keep_local and local_archive.exists():
            local_archive.unlink()
        try:
            temp_dir.rmdir()
        except OSError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replicate uswest control-server backup archive to Cudy fallback storage.")
    parser.add_argument("--source-host", default=DEFAULT_SOURCE_HOST)
    parser.add_argument("--source-user", default=DEFAULT_SOURCE_USER)
    parser.add_argument("--source-password")
    parser.add_argument("--source-remote-dir", default=DEFAULT_SOURCE_REMOTE_DIR)
    parser.add_argument("--cudy-host", default=DEFAULT_CUDY_HOST)
    parser.add_argument("--cudy-user", default=DEFAULT_CUDY_USER)
    parser.add_argument("--cudy-password")
    parser.add_argument("--cudy-state-dir", default=DEFAULT_CUDY_STATE_DIR)
    parser.add_argument("--cudy-web-dir", default=DEFAULT_CUDY_WEB_DIR)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--keep-remote", type=int, default=3, help="Keep newest N secret archives on Cudy.")
    parser.add_argument("--keep-local", action="store_true")
    parser.add_argument("--no-secrets", action="store_true", help="Do not include secrets/ in the replicated archive.")
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = sync(args)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Replicated: {result['remote_archive']}")
        print(f"Bytes: {result['bytes']}")
        print(f"SHA256: {result['sha256']}")
        print("Public status: /cudy-control/state.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
