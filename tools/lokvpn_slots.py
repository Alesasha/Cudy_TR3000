#!/usr/bin/env python3
"""
Manage dynamic LokVPN slots on Cudy.

A slot is an independent sing-box TUN interface such as lok1 or lok2 that is
bound to one logical LokVPN profile such as de1 or fr2. This is a prototype
control layer for per-user LokVPN routing before the Go daemon takes over.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

import paramiko


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CUDY_HOST = "192.168.8.1"
DEFAULT_CUDY_USER = "root"
DEFAULT_CUDY_PASSWORD_FILE = ROOT / "secrets" / "cudy_ssh_password.txt"
LOCAL_LOKVPN_REFRESH = ROOT / "openwrt" / "lokvpn-refresh"
LOCAL_LOKVPN_SLOT = ROOT / "openwrt" / "lokvpn-slot"
REMOTE_LOKVPN_REFRESH = "/usr/bin/lokvpn-refresh"
REMOTE_LOKVPN_SLOT = "/usr/bin/lokvpn-slot"
LOKVPN_PROFILES = {
    "smart1",
    "de1",
    "ru1",
    "nl1",
    "fr1",
    "se1",
    "smart2",
    "de2",
    "ru2",
    "nl2",
    "fr2",
    "se2",
}


def load_cudy_ssh_password(explicit_password: str | None = None) -> str | None:
    if explicit_password:
        return explicit_password
    env_password = os.environ.get("CUDY_SSH_PASSWORD")
    if env_password:
        return env_password
    if DEFAULT_CUDY_PASSWORD_FILE.exists():
        password = DEFAULT_CUDY_PASSWORD_FILE.read_text(encoding="utf-8-sig").strip()
        if password:
            return password
    return None


def ssh_connect(host: str, user: str, password: str, timeout: int) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        username=user,
        password=password,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def ssh_exec(client: paramiko.SSHClient, command: str, timeout: int) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def ssh_exec_checked(client: paramiko.SSHClient, command: str, timeout: int) -> str:
    rc, out, err = ssh_exec(client, command, timeout)
    if rc != 0:
        raise RuntimeError(f"Remote command failed rc={rc}: {command}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    return out


def ssh_upload_file(client: paramiko.SSHClient, local: Path, remote: str, timeout: int) -> None:
    payload = local.read_bytes()
    command = f"cat > {shlex.quote(remote)}"
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    stdin.channel.sendall(payload)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        raise RuntimeError(f"Remote upload failed rc={rc}: {remote}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    ssh_exec_checked(client, f"chmod +x {shlex.quote(remote)}", timeout)


def parse_slot_table(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return rows
    header = lines[0].split("\t")
    for line in lines[1:]:
        values = line.split("\t")
        item = {key: values[idx] if idx < len(values) else "" for idx, key in enumerate(header)}
        rows.append(item)
    return rows


def print_slot_table(rows: list[dict[str, str]]) -> None:
    if not rows:
        print("No LokVPN slots.")
        return
    columns = ["slot", "profile", "address", "service", "link", "updated_at"]
    widths = {col: max(len(col), *(len(str(row.get(col, ""))) for row in rows)) for col in columns}
    print("  ".join(col.ljust(widths[col]) for col in columns))
    print("  ".join("-" * widths[col] for col in columns))
    for row in rows:
        print("  ".join(str(row.get(col, "")).ljust(widths[col]) for col in columns))


def profile_from_server_id(value: str) -> str:
    raw = value.strip()
    if raw.startswith("lokvpn-"):
        raw = raw.removeprefix("lokvpn-")
    if raw not in LOKVPN_PROFILES:
        raise ValueError(f"Unknown LokVPN profile: {value}")
    return raw


def connect_from_args(args: argparse.Namespace) -> paramiko.SSHClient:
    password = load_cudy_ssh_password(args.ssh_password)
    if not password:
        password = getpass.getpass(f"SSH password for {args.ssh_user}@{args.ssh_host}: ")
    return ssh_connect(args.ssh_host, args.ssh_user, password, args.ssh_timeout)


def install_scripts(client: paramiko.SSHClient, timeout: int) -> None:
    ssh_upload_file(client, LOCAL_LOKVPN_REFRESH, REMOTE_LOKVPN_REFRESH, timeout)
    ssh_upload_file(client, LOCAL_LOKVPN_SLOT, REMOTE_LOKVPN_SLOT, timeout)
    ssh_exec_checked(
        client,
        f"sh -n {shlex.quote(REMOTE_LOKVPN_REFRESH)} && sh -n {shlex.quote(REMOTE_LOKVPN_SLOT)}",
        timeout,
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ssh-host", default=DEFAULT_CUDY_HOST)
    parser.add_argument("--ssh-user", default=DEFAULT_CUDY_USER)
    parser.add_argument("--ssh-password")
    parser.add_argument("--ssh-timeout", type=int, default=120)
    parser.add_argument("--install-scripts", action="store_true", help="Upload lokvpn-refresh and lokvpn-slot first.")
    parser.add_argument("--json", action="store_true", help="Print JSON where supported.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage dynamic LokVPN slots on Cudy.")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List active LokVPN slots.")
    add_common_args(list_parser)

    ensure_parser = sub.add_parser("ensure", help="Ensure a slot exists for a LokVPN profile.")
    ensure_parser.add_argument("profile", help="Profile such as de1, fr2, or server id lokvpn-de1.")
    ensure_parser.add_argument("--slot", help="Optional explicit slot name such as lok1.")
    add_common_args(ensure_parser)

    remove_parser = sub.add_parser("remove", help="Remove a slot.")
    remove_parser.add_argument("slot")
    add_common_args(remove_parser)

    gc_parser = sub.add_parser("gc", help="Remove slots whose profiles are not in the keep list.")
    gc_parser.add_argument(
        "keep_profiles",
        nargs="?",
        default="",
        help="Comma-separated profiles or LokVPN server ids to keep. Blank removes all slots.",
    )
    add_common_args(gc_parser)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        client = connect_from_args(args)
    except Exception as exc:
        print(f"SSH connection failed: {exc}", file=sys.stderr)
        return 1
    try:
        if args.install_scripts:
            install_scripts(client, args.ssh_timeout)

        if args.command == "list":
            out = ssh_exec_checked(client, f"{REMOTE_LOKVPN_SLOT} list", args.ssh_timeout)
            rows = parse_slot_table(out)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print_slot_table(rows)
            return 0

        if args.command == "ensure":
            profile = profile_from_server_id(args.profile)
            command = f"{REMOTE_LOKVPN_SLOT} ensure {shlex.quote(profile)}"
            if args.slot:
                command += f" {shlex.quote(args.slot)}"
            out = ssh_exec_checked(client, command, args.ssh_timeout)
            print(out.strip())
            return 0

        if args.command == "remove":
            out = ssh_exec_checked(client, f"{REMOTE_LOKVPN_SLOT} remove {shlex.quote(args.slot)}", args.ssh_timeout)
            print(out.strip())
            return 0

        if args.command == "gc":
            profiles = [profile_from_server_id(item) for item in args.keep_profiles.split(",") if item.strip()]
            out = ssh_exec_checked(client, f"{REMOTE_LOKVPN_SLOT} gc {shlex.quote(','.join(profiles))}", args.ssh_timeout)
            print(out.strip())
            return 0
    except Exception as exc:
        message = str(exc) or repr(exc)
        print(message, file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
