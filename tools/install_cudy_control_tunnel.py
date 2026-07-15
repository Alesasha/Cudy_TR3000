#!/usr/bin/env python3
"""Install the optional Cudy -> control-server SSH tunnel service.

This prepares `/etc/init.d/cudy-control-tunnel` and its root-only config on
Cudy. It does not create the remote SSH user or authorize the key on uswest.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from typing import Any

from deploy_cudy_go_fallback import ROOT, connect, load_password, ssh_exec, upload_via_cat


DEFAULT_HOST = "192.168.8.1"
DEFAULT_USER = "root"
DEFAULT_REMOTE_DIR = "/etc/cudy-fallback"
DEFAULT_INIT = ROOT / "openwrt" / "cudy-control-tunnel.init"


def render_env(args: argparse.Namespace) -> str:
    values = {
        "CONTROL_HOST": args.control_host,
        "CONTROL_PORT": str(args.control_port),
        "CONTROL_USER": args.control_user,
        "CONTROL_LOCAL_PORT": str(args.local_port),
        "CONTROL_REMOTE_HOST": args.remote_host,
        "CONTROL_REMOTE_PORT": str(args.remote_port),
        "CONTROL_IDENTITY_FILE": f"{args.remote_dir.rstrip('/')}/control_tunnel_ed25519",
    }
    return "".join(f"{key}={shell_quote(value)}\n" for key, value in values.items())


def install(args: argparse.Namespace) -> dict[str, Any]:
    identity = Path(args.identity_file) if args.identity_file else None
    remote_dir = args.remote_dir.rstrip("/")
    remote_key = f"{remote_dir}/control_tunnel_ed25519"
    remote_env = f"{remote_dir}/tunnel.env"

    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "host": args.host,
            "init": "/etc/init.d/cudy-control-tunnel",
            "env": remote_env,
            "identity": remote_key,
            "control_host": args.control_host,
            "control_user": args.control_user,
            "local_port": args.local_port,
        }

    if identity is None:
        raise SystemExit("--identity-file is required unless --dry-run is used")
    if not identity.exists():
        raise SystemExit(f"identity file not found: {identity}")
    if not DEFAULT_INIT.exists():
        raise SystemExit(f"init template not found: {DEFAULT_INIT}")

    password = load_password(args.ssh_password)
    client = connect(args.host, args.user, password, args.timeout)
    try:
        with tempfile.TemporaryDirectory(prefix="cudy-control-tunnel-") as tmp_raw:
            tmp = Path(tmp_raw)
            env_path = tmp / "tunnel.env"
            # OpenWrt sources this file with /bin/sh. Write explicit LF bytes;
            # Path.write_text on Windows would otherwise emit CRLF and leave
            # a literal carriage return in host/user option values.
            env_path.write_bytes(render_env(args).encode("utf-8"))
            upload_via_cat(client, DEFAULT_INIT, "/tmp/cudy-control-tunnel.init")
            upload_via_cat(client, env_path, "/tmp/cudy-control-tunnel.env")
            upload_via_cat(client, identity, "/tmp/cudy-control-tunnel.key")

        action = "restart" if args.start else "stop"
        enable = "enable" if args.enable else "disable"
        rc, output = ssh_exec(
            client,
            f"""
set -eu
umask 077
mkdir -p {shell_quote(remote_dir)}
chmod 0700 {shell_quote(remote_dir)}
mv /tmp/cudy-control-tunnel.env {shell_quote(remote_env)}
mv /tmp/cudy-control-tunnel.key {shell_quote(remote_key)}
mv /tmp/cudy-control-tunnel.init /etc/init.d/cudy-control-tunnel
chmod 0600 {shell_quote(remote_env)} {shell_quote(remote_key)}
chmod 0755 /etc/init.d/cudy-control-tunnel
chown root:root {shell_quote(remote_env)} {shell_quote(remote_key)} /etc/init.d/cudy-control-tunnel
/etc/init.d/cudy-control-tunnel {enable}
/etc/init.d/cudy-control-tunnel {action} || true
/etc/init.d/cudy-control-tunnel status || true
if command -v ss >/dev/null 2>&1; then ss -ltnp 2>/dev/null | grep ':{args.local_port} ' || true; fi
""".strip(),
            args.timeout,
        )
    finally:
        client.close()

    return {
        "ok": rc == 0,
        "host": args.host,
        "enabled": bool(args.enable),
        "started": bool(args.start),
        "env": remote_env,
        "identity": remote_key,
        "output": output,
    }


def shell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--ssh-password")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    parser.add_argument("--control-host", default=os.environ.get("VPN_CONTROL_PRIMARY_SSH_HOST", "95.182.91.203"))
    parser.add_argument("--control-port", type=int, default=22)
    parser.add_argument("--control-user", default=os.environ.get("VPN_CONTROL_PRIMARY_SSH_USER", "cudy-tunnel-linux"))
    parser.add_argument("--local-port", type=int, default=18765)
    parser.add_argument("--remote-host", default="127.0.0.1")
    parser.add_argument("--remote-port", type=int, default=8765)
    parser.add_argument("--identity-file")
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = install(args)
    if args.json:
        import json

        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Cudy control tunnel install: {'OK' if result.get('ok') else 'FAIL'} host={result.get('host')}")
        if result.get("dry_run"):
            print(f"  init={result.get('init')}")
            print(f"  env={result.get('env')}")
            print(f"  identity={result.get('identity')}")
            print(
                "  tunnel="
                f"127.0.0.1:{result.get('local_port')} -> "
                f"{result.get('control_user')}@{result.get('control_host')}:127.0.0.1:8765"
            )
        else:
            print(f"  enabled={result.get('enabled')} started={result.get('started')}")
            print(f"  env={result.get('env')}")
            print(f"  identity={result.get('identity')}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
