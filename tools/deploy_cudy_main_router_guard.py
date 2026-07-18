#!/usr/bin/env python3
"""Install the disarmed Cudy main-router cutover guard."""

from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
from typing import Any

from deploy_cudy_go_fallback import ROOT, connect, load_password, ssh_exec, upload_via_cat


FILES = {
    ROOT / "openwrt" / "cudy-main-router-guard": "/usr/bin/cudy-main-router-guard",
    ROOT / "openwrt" / "cudy-main-router-guard.init": "/etc/init.d/cudy-main-router-guard",
}


def parse_status(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    return fields


def deploy(args: argparse.Namespace) -> dict[str, Any]:
    ipaddress.ip_address(args.expected_lan_ip)
    ipaddress.ip_address(args.expected_wan_gateway)
    if not 1 <= args.failure_threshold <= 10:
        raise ValueError("failure threshold must be between 1 and 10")
    for source in FILES:
        if not source.is_file():
            raise FileNotFoundError(source)
    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "host": args.host,
            "backup_now": args.backup_now,
            "expected_lan_ip": args.expected_lan_ip,
            "expected_wan_gateway": args.expected_wan_gateway,
            "files": [str(path) for path in FILES],
        }

    if args.check_only:
        client = connect(args.host, args.user, load_password(args.ssh_password), args.timeout)
        try:
            rc, output = ssh_exec(
                client,
                "printf 'service='; /etc/init.d/cudy-main-router-guard status 2>/dev/null || true; "
                "printf '\\n'; /usr/bin/cudy-main-router-guard status 2>/dev/null || true",
                args.timeout,
            )
        finally:
            client.close()
        fields = parse_status(output)
        return {
            "ok": rc == 0
            and fields.get("service") == "running"
            and fields.get("armed") == "no"
            and fields.get("backup") == "valid",
            "host": args.host,
            "check_only": True,
            "fields": fields,
            "output": output,
        }

    client = connect(args.host, args.user, load_password(args.ssh_password), args.timeout)
    try:
        for index, source in enumerate(FILES):
            upload_via_cat(client, source, f"/tmp/cudy-main-router-guard-{index}")
        backup_command = "/usr/bin/cudy-main-router-guard backup" if args.backup_now else "true"
        rc, output = ssh_exec(
            client,
            f"""
set -eu
ash -n /tmp/cudy-main-router-guard-0
ash -n /tmp/cudy-main-router-guard-1
cp /tmp/cudy-main-router-guard-0 /usr/bin/cudy-main-router-guard
cp /tmp/cudy-main-router-guard-1 /etc/init.d/cudy-main-router-guard
chmod 0755 /usr/bin/cudy-main-router-guard /etc/init.d/cudy-main-router-guard
mkdir -p /var/lib/cudy-main-router-guard
chmod 0700 /var/lib/cudy-main-router-guard
touch /etc/config/cudy-main-router-guard
if ! uci -q get cudy-main-router-guard.main >/dev/null; then
  uci set cudy-main-router-guard.main='guard'
fi
uci set cudy-main-router-guard.main.expected_lan_ip='{args.expected_lan_ip}'
uci set cudy-main-router-guard.main.expected_wan_gateway='{args.expected_wan_gateway}'
uci set cudy-main-router-guard.main.failure_threshold='{args.failure_threshold}'
uci commit cudy-main-router-guard
/etc/init.d/cudy-main-router-guard enable
/etc/init.d/cudy-main-router-guard restart
{backup_command}
printf 'service='; /etc/init.d/cudy-main-router-guard status 2>/dev/null || true
printf '\n'; /usr/bin/cudy-main-router-guard status
""".strip(),
            args.timeout,
        )
    finally:
        client.close()

    fields = parse_status(output)
    ok = rc == 0 and fields.get("service") == "running" and fields.get("armed") == "no"
    if args.backup_now:
        ok = ok and fields.get("backup") == "valid"
    return {"ok": ok, "host": args.host, "fields": fields, "output": output}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.8.1")
    parser.add_argument("--user", default="root")
    parser.add_argument("--ssh-password")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--expected-lan-ip", default="192.168.1.1")
    parser.add_argument("--expected-wan-gateway", default="195.170.35.1")
    parser.add_argument("--failure-threshold", type=int, default=3)
    parser.add_argument("--backup-now", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = deploy(args)
    if result.get("dry_run") or args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Cudy main-router guard: {'OK' if result.get('ok') else 'FAIL'} host={result.get('host')}")
        for key, value in (result.get("fields") or {}).items():
            print(f"  {key}={value}")
        if not result.get("ok") and result.get("output"):
            print(result["output"])
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
