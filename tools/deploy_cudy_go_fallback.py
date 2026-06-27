#!/usr/bin/env python3
"""Deploy the Cudy Go fallback binary to OpenWrt.

OpenWrt on the current Cudy does not expose a working SFTP subsystem, so this
tool uploads files through `cat > /tmp/file` over SSH exec channels.
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
DEFAULT_HOST = "192.168.8.1"
DEFAULT_USER = "root"
DEFAULT_PASSWORD_FILE = ROOT / "secrets" / "cudy_ssh_password.txt"
DEFAULT_BINARY = ROOT / "build" / "cudy" / "cudy-fallback-linux-arm64"
DEFAULT_INIT = ROOT / "openwrt" / "cudy-fallback.init"


def load_password(explicit: str | None) -> str:
    if explicit:
        return explicit
    value = os.environ.get("CUDY_SSH_PASSWORD")
    if value:
        return value
    if DEFAULT_PASSWORD_FILE.exists():
        value = DEFAULT_PASSWORD_FILE.read_text(encoding="utf-8-sig").strip()
        if value:
            return value
    return getpass.getpass("Cudy SSH password: ")


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


def ssh_exec(client: paramiko.SSHClient, command: str, timeout: int) -> tuple[int, str]:
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    return rc, (out + err).strip()


def upload_via_cat(client: paramiko.SSHClient, local_path: Path, remote_path: str) -> None:
    data = local_path.read_bytes()
    channel = client.get_transport().open_session()
    channel.exec_command(f"cat > {shlex.quote(remote_path)}")
    channel.sendall(data)
    channel.shutdown_write()
    rc = channel.recv_exit_status()
    if rc != 0:
        raise RuntimeError(f"upload failed for {remote_path}: rc={rc}")


def parse_json_line(output: str, prefix: str) -> dict[str, Any]:
    for line in output.splitlines():
        if line.startswith(prefix):
            return json.loads(line[len(prefix) :])
    raise ValueError(f"missing {prefix!r} line in output")


def deploy(args: argparse.Namespace) -> dict[str, Any]:
    binary = Path(args.binary).resolve()
    init = Path(args.init).resolve()
    if not binary.exists():
        raise FileNotFoundError(f"binary does not exist: {binary}")
    if not init.exists():
        raise FileNotFoundError(f"init script does not exist: {init}")

    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "host": args.host,
            "binary": str(binary),
            "init": str(init),
            "actions": [
                "upload binary to /tmp/cudy-fallback",
                "upload init to /tmp/cudy-fallback.init",
                "install to /usr/bin and /etc/init.d",
                "enable and restart cudy-fallback",
                "check /readyz and /api/cudy/runtime",
            ],
        }

    password = load_password(args.ssh_password)
    client = connect(args.host, args.user, password, args.timeout)
    try:
        upload_via_cat(client, binary, "/tmp/cudy-fallback")
        upload_via_cat(client, init, "/tmp/cudy-fallback.init")
        command = """
set -eu
cp /tmp/cudy-fallback /usr/bin/cudy-fallback
cp /tmp/cudy-fallback.init /etc/init.d/cudy-fallback
chmod 0755 /usr/bin/cudy-fallback /etc/init.d/cudy-fallback
/etc/init.d/cudy-fallback enable
/etc/init.d/cudy-fallback restart
sleep 1
printf 'service='
/etc/init.d/cudy-fallback status || true
printf '\\nready='
curl -fsS --max-time 5 http://127.0.0.1:8765/readyz
printf '\\nruntime='
curl -fsS --max-time 10 http://127.0.0.1:8765/api/cudy/runtime
printf '\\n'
""".strip()
        rc, output = ssh_exec(client, command, args.timeout)
    finally:
        client.close()
    if rc != 0:
        return {"ok": False, "host": args.host, "error": output}

    lines = output.splitlines()
    service = next((line.split("=", 1)[1] for line in lines if line.startswith("service=")), "")
    ready = parse_json_line(output, "ready=")
    runtime = parse_json_line(output, "runtime=")
    return {
        "ok": service == "running" and bool(ready.get("ok")) and bool(runtime.get("ok")),
        "dry_run": False,
        "host": args.host,
        "service": service,
        "readyz": {"ok": ready.get("ok"), "warnings": ready.get("warnings") or []},
        "runtime": {
            "ok": runtime.get("ok"),
            "architecture": runtime.get("architecture"),
            "openwrt_target": runtime.get("openwrt_target"),
            "supported_interfaces": len(runtime.get("supported_interfaces") or []),
            "links": len(runtime.get("links") or []),
            "warnings": runtime.get("warnings") or [],
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--ssh-password")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--binary", default=str(DEFAULT_BINARY))
    parser.add_argument("--init", default=str(DEFAULT_INIT))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = deploy(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Cudy Go fallback deploy: {'OK' if result.get('ok') else 'FAIL'} host={result.get('host')}")
        if result.get("dry_run"):
            for action in result.get("actions") or []:
                print(f"  - {action}")
        elif result.get("ok"):
            runtime = result.get("runtime") or {}
            print(f"  service={result.get('service')}")
            print(f"  readyz={result.get('readyz')}")
            print(
                "  runtime="
                f"arch={runtime.get('architecture')} target={runtime.get('openwrt_target')} "
                f"interfaces={runtime.get('supported_interfaces')} links={runtime.get('links')}"
            )
        else:
            print(f"  error={result.get('error')}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
