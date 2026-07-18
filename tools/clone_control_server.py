#!/usr/bin/env python3
"""Clone a live Cudy control-server to a replacement VPS.

The clone includes the remote SQLite database, provider secrets, agent tokens,
transport cache, config, tools, and systemd unit. By default the source service
is stopped briefly while the archive is created so SQLite/WAL files are copied
consistently.
"""

from __future__ import annotations

import argparse
import getpass
import os
import posixpath
import shlex
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

import paramiko


DEFAULT_SOURCE_HOST = "95.182.91.203"
DEFAULT_USER = "root"
DEFAULT_REMOTE_DIR = "/opt/cudy-control"
DEFAULT_SERVICE = "vpn-control"
DEFAULT_SERVICE_USER = "cudy-control"


def password_from_env_or_prompt(explicit: str | None, *, env_names: tuple[str, ...], prompt: str) -> str:
    if explicit:
        return explicit
    for name in env_names:
        value = os.environ.get(name)
        if value:
            return value
    return getpass.getpass(prompt)


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
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        raise RuntimeError(f"remote command failed rc={rc}: {command}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    return out + err


def remote_file_exists(sftp: paramiko.SFTPClient, path: str) -> bool:
    try:
        mode = sftp.stat(path).st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISREG(mode)


def create_source_archive(
    client: paramiko.SSHClient,
    *,
    remote_dir: str,
    service_name: str,
    stop_source: bool,
    timeout: int,
) -> str:
    timestamp = int(time.time())
    archive = f"/tmp/cudy-control-clone-{timestamp}.tgz"
    quoted_service = shlex.quote(service_name)
    quoted_dir = shlex.quote(remote_dir)
    quoted_archive = shlex.quote(archive)
    stop_start = ""
    if stop_source:
        stop_start = (
            f"if systemctl list-unit-files {quoted_service}.service >/dev/null 2>&1; then "
            f"systemctl stop {quoted_service}; "
            "fi\n"
        )
    start_after = ""
    if stop_source:
        start_after = (
            f"if systemctl list-unit-files {quoted_service}.service >/dev/null 2>&1; then "
            f"systemctl start {quoted_service}; "
            "fi\n"
        )
    command = (
        "set -eu\n"
        f"test -d {quoted_dir}\n"
        f"{stop_start}"
        f"tar -C {quoted_dir} -czf {quoted_archive} .\n"
        f"{start_after}"
        f"ls -lh {quoted_archive}\n"
    )
    try:
        ssh_exec(client, command, timeout)
    except Exception:
        if stop_source:
            ssh_exec(
                client,
                f"systemctl start {quoted_service} >/dev/null 2>&1 || true",
                max(10, timeout),
            )
        raise
    return archive


def prepare_target(
    client: paramiko.SSHClient,
    *,
    remote_dir: str,
    service_user: str,
    timeout: int,
) -> None:
    command = (
        "set -eu\n"
        "if command -v apt-get >/dev/null 2>&1; then "
        "apt-get update -y && apt-get install -y python3 python3-paramiko openssh-client curl tar; "
        "fi\n"
        f"id -u {shlex.quote(service_user)} >/dev/null 2>&1 || "
        f"useradd --system --home {shlex.quote(remote_dir)} --shell /usr/sbin/nologin {shlex.quote(service_user)}\n"
        f"mkdir -p {shlex.quote(remote_dir)}\n"
    )
    ssh_exec(client, command, timeout * 6)


def install_target(
    client: paramiko.SSHClient,
    *,
    local_archive: Path,
    remote_dir: str,
    service_name: str,
    service_user: str,
    timeout: int,
) -> str:
    remote_archive = f"/tmp/{local_archive.name}"
    sftp = client.open_sftp()
    try:
        sftp.put(str(local_archive), remote_archive)
    finally:
        sftp.close()
    command = (
        "set -eu\n"
        f"systemctl stop {shlex.quote(service_name)} >/dev/null 2>&1 || true\n"
        f"mkdir -p {shlex.quote(remote_dir)}\n"
        f"tar -C {shlex.quote(remote_dir)} -xzf {shlex.quote(remote_archive)}\n"
        f"rm -f {shlex.quote(remote_archive)}\n"
        f"chown -R {shlex.quote(service_user)}:{shlex.quote(service_user)} {shlex.quote(remote_dir)}\n"
        f"python3 {shlex.quote(posixpath.join(remote_dir, 'tools/install_agent_provisioning_ssh.py'))} "
        f"--service-user {shlex.quote(service_user)} "
        f"--keys-path {shlex.quote(posixpath.join(remote_dir, 'data/agent_authorized_keys'))}\n"
        f"cp {shlex.quote(posixpath.join(remote_dir, 'deploy/uswest/vpn-control.service'))} "
        f"/etc/systemd/system/{shlex.quote(service_name)}.service\n"
        "systemctl daemon-reload\n"
        f"systemctl enable --now {shlex.quote(service_name)}\n"
        f"systemctl restart {shlex.quote(service_name)}\n"
        "for i in $(seq 1 45); do "
        "curl -fsS http://127.0.0.1:8765/healthz >/tmp/vpn-control-health.json 2>/tmp/vpn-control-health.err && break; "
        "sleep 1; "
        "done\n"
        f"systemctl --no-pager --full status {shlex.quote(service_name)} | head -45\n"
        "cat /tmp/vpn-control-health.json\n"
    )
    return ssh_exec(client, command, timeout * 4)


def clone(args: argparse.Namespace) -> dict[str, Any]:
    if args.remote_dir != DEFAULT_REMOTE_DIR:
        raise ValueError(
            "custom --remote-dir is not supported yet because the systemd unit "
            f"is pinned to {DEFAULT_REMOTE_DIR}"
        )
    target_password = password_from_env_or_prompt(
        args.target_password,
        env_names=("TARGET_SSH_PASSWORD",),
        prompt=f"SSH password for target {args.target_host}: ",
    )
    source_archive = ""
    source: paramiko.SSHClient | None = None
    keep_local_archive = args.keep_archive
    if args.source_archive:
        local_archive = Path(args.source_archive).resolve()
        if not local_archive.is_file():
            raise FileNotFoundError(f"backup archive not found: {local_archive}")
        source_label = f"archive:{local_archive}"
        keep_local_archive = True
    else:
        source_password = password_from_env_or_prompt(
            args.source_password,
            env_names=("SOURCE_SSH_PASSWORD", "USWEST_SSH_PASSWORD", "AWG_SSH_PASSWORD_HOSTVDS_USWEST", "AWG_SSH_PASSWORD"),
            prompt=f"SSH password for source {args.source_host}: ",
        )
        archive_dir = Path(args.archive_dir)
        archive_dir.mkdir(parents=True, exist_ok=True)
        local_archive = archive_dir / f"cudy-control-clone-{int(time.time())}.tgz"
        source_label = args.source_host
        source = connect(args.source_host, args.source_user, source_password, args.timeout, attempts=args.connect_attempts)
    target = connect(args.target_host, args.target_user, target_password, args.timeout, attempts=args.connect_attempts)
    try:
        if source is not None:
            source_archive = create_source_archive(
                source,
                remote_dir=args.remote_dir,
                service_name=args.service_name,
                stop_source=not args.no_stop_source,
                timeout=args.timeout * 3,
            )
            sftp_source = source.open_sftp()
            try:
                if not remote_file_exists(sftp_source, source_archive):
                    raise RuntimeError(f"source archive was not created: {source_archive}")
                sftp_source.get(source_archive, str(local_archive))
            finally:
                sftp_source.close()
        prepare_target(
            target,
            remote_dir=args.remote_dir,
            service_user=args.service_user,
            timeout=args.timeout,
        )
        output = install_target(
            target,
            local_archive=local_archive,
            remote_dir=args.remote_dir,
            service_name=args.service_name,
            service_user=args.service_user,
            timeout=args.timeout,
        )
    finally:
        if source is not None and source_archive:
            try:
                ssh_exec(source, f"rm -f {shlex.quote(source_archive)}", args.timeout)
            except Exception:
                pass
        if source is not None:
            source.close()
        target.close()
        if local_archive.exists() and not keep_local_archive:
            local_archive.unlink()
    return {
        "source_host": source_label,
        "target_host": args.target_host,
        "remote_dir": args.remote_dir,
        "archive": str(local_archive) if keep_local_archive else "",
        "target_output": output,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One-click clone Cudy control-server to a new VPS.")
    parser.add_argument("--source-host", default=DEFAULT_SOURCE_HOST)
    parser.add_argument("--source-user", default=DEFAULT_USER)
    parser.add_argument("--source-password")
    parser.add_argument("--source-archive", help="Restore target from an existing local backup/clone archive instead of connecting to source.")
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-user", default=DEFAULT_USER)
    parser.add_argument("--target-password")
    parser.add_argument("--connect-attempts", type=int, default=3)
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    parser.add_argument("--service-name", default=DEFAULT_SERVICE)
    parser.add_argument("--service-user", default=DEFAULT_SERVICE_USER)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--archive-dir", default=str(Path("build") / "control-clones"))
    parser.add_argument("--keep-archive", action="store_true", help="Keep the local sensitive tarball after upload.")
    parser.add_argument("--no-stop-source", action="store_true", help="Do not stop the source service while archiving.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = clone(args)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Cloned {result['source_host']} -> {result['target_host']}:{result['remote_dir']}")
    if result["archive"]:
        print(f"Sensitive local archive kept: {result['archive']}")
    print(result["target_output"])
    print(f"Open tunnel: ssh -N -L 8765:127.0.0.1:8765 {args.target_user}@{args.target_host}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
