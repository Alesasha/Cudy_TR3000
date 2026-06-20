#!/usr/bin/env python3
"""Publish the control endpoint manifest to Cudy static web storage.

This is the first lightweight fallback-control layer. Cudy does not need to run
the full Python control-server for this step: agents can fetch a static
`endpoints.json` from Cudy and learn where the current primary control-server is
after uswest is rebuilt or moved.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
from pathlib import Path

import paramiko

from vpn_control_app import control_endpoints_manifest


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CUDY_PASSWORD_FILE = ROOT / "secrets" / "cudy_ssh_password.txt"
DEFAULT_CUDY_HOST = "192.168.8.1"
DEFAULT_CUDY_USER = "root"
DEFAULT_REMOTE_DIR = "/www/cudy-control"


def password_from_env_or_prompt(explicit: str | None, *, host: str) -> str:
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


def ssh_write_file(client: paramiko.SSHClient, path: str, content: str, timeout: int) -> None:
    command = f"cat > {shlex.quote(path)}"
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    stdin.write(content)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        raise RuntimeError(f"remote write failed rc={rc}: {path}\nSTDOUT:\n{out}\nSTDERR:\n{err}")


def publish(args: argparse.Namespace) -> dict[str, str]:
    manifest = control_endpoints_manifest()
    payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if args.dry_run:
        return {
            "mode": "dry-run",
            "remote_path": f"{args.remote_dir}/endpoints.json",
            "manifest": payload,
        }

    password = password_from_env_or_prompt(args.ssh_password, host=args.host)
    client = connect(args.host, args.user, password, args.timeout)
    try:
        remote_dir = args.remote_dir.rstrip("/")
        remote_path = f"{remote_dir}/endpoints.json"
        ssh_exec(client, f"mkdir -p {shlex.quote(remote_dir)}", args.timeout)
        ssh_write_file(client, remote_path, payload, args.timeout)
        ssh_exec(client, f"chmod 0644 {shlex.quote(remote_path)} && ls -l {shlex.quote(remote_path)}", args.timeout)
        return {
            "mode": "published",
            "remote_path": remote_path,
            "url_lan": f"http://{args.host}/cudy-control/endpoints.json",
            "url_vpn": "http://10.77.0.1/cudy-control/endpoints.json",
        }
    finally:
        client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish control endpoint manifest to Cudy static web storage.")
    parser.add_argument("--host", default=DEFAULT_CUDY_HOST)
    parser.add_argument("--user", default=DEFAULT_CUDY_USER)
    parser.add_argument("--ssh-password")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = publish(args)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{result['mode']}: {result['remote_path']}")
        if result["mode"] == "published":
            print(f"LAN URL: {result['url_lan']}")
            print(f"VPN URL: {result['url_vpn']}")
        else:
            print(result["manifest"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
