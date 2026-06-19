#!/usr/bin/env python3
"""Measure Cudy VPN/proxy exits through selected interfaces.

The script runs curl on Cudy with --interface, so it measures the route as seen
by the router rather than by the operator PC.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import shlex
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paramiko


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "config" / "vpn_inventory.json"
DEFAULT_CUDY_HOST = "192.168.8.1"
DEFAULT_CUDY_USER = "root"
DEFAULT_CUDY_PASSWORD_FILE = ROOT / "secrets" / "cudy_ssh_password.txt"
DEFAULT_URLS = [
    "https://ifconfig.me/ip",
    "https://www.speedtest.net/",
    "https://web.telegram.org/",
]


@dataclass(frozen=True)
class ExitTarget:
    server_id: str
    label: str
    interface: str
    provider: str
    kind: str


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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


def inventory_targets(inventory: dict[str, Any]) -> dict[str, ExitTarget]:
    result: dict[str, ExitTarget] = {}
    for item in inventory.get("servers", []):
        if not item.get("enabled", False):
            continue
        iface = item.get("interface")
        if not iface:
            continue
        server_id = str(item["id"])
        result[server_id] = ExitTarget(
            server_id=server_id,
            label=str(item.get("label") or server_id),
            interface=str(iface),
            provider=str(item.get("provider") or ""),
            kind=str(item.get("kind") or ""),
        )
    return result


def parse_csv_arg(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def resolve_targets(inventory: dict[str, Any], selected: list[str]) -> list[ExitTarget]:
    known = inventory_targets(inventory)
    if not selected:
        selected = ["aktau", "uswest", "proxyru", "proxyde", "proxykz", "proxyus"]
    targets: list[ExitTarget] = []
    for name in selected:
        if name in known:
            targets.append(known[name])
            continue
        # Allow raw interface names for operational tests.
        targets.append(ExitTarget(server_id=name, label=name, interface=name, provider="manual", kind="interface"))
    return targets


def ssh_connect(host: str, user: str, password: str, timeout: int) -> paramiko.SSHClient:
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


def parse_key_values(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def run_remote(client: paramiko.SSHClient, command: str, timeout: int) -> tuple[int, str]:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    return rc, (out + ("\n" + err if err.strip() else "")).strip()


def curl_probe(
    client: paramiko.SSHClient,
    *,
    iface: str,
    url: str,
    connect_timeout: int,
    max_time: int,
    timeout: int,
) -> dict[str, Any]:
    command = (
        "out=$(curl -4 -L -sS -o /dev/null "
        f"--interface {shlex.quote(iface)} "
        f"--connect-timeout {int(connect_timeout)} --max-time {int(max_time)} "
        "-w 'http_code=%{http_code}\\ntime_namelookup=%{time_namelookup}\\n"
        "time_connect=%{time_connect}\\ntime_starttransfer=%{time_starttransfer}\\n"
        "time_total=%{time_total}\\nremote_ip=%{remote_ip}\\n"
        "size_download=%{size_download}\\nspeed_download=%{speed_download}\\n' "
        f"{shlex.quote(url)} 2>&1); "
        "rc=$?; printf 'rc=%s\\n%s\\n' \"$rc\" \"$out\""
    )
    _, output = run_remote(client, command, timeout)
    parsed = parse_key_values(output)
    parsed["raw"] = output
    return parsed


def egress_ip(
    client: paramiko.SSHClient,
    *,
    iface: str,
    connect_timeout: int,
    max_time: int,
    timeout: int,
) -> str:
    command = (
        "curl -4 -L -sS "
        f"--interface {shlex.quote(iface)} "
        f"--connect-timeout {int(connect_timeout)} --max-time {int(max_time)} "
        "https://ifconfig.me/ip 2>/dev/null | tr -d '\\r\\n' || true"
    )
    _, output = run_remote(client, command, timeout)
    return output.strip()


def to_float_ms(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value) * 1000)
    except ValueError:
        return None


def to_int(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def render_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No results.")
        return
    headers = ["server", "iface", "url", "rc", "http", "total_ms", "ttfb_ms", "mbps", "egress_ip", "remote_ip"]
    widths = {header: len(header) for header in headers}
    rendered: list[dict[str, str]] = []
    for row in rows:
        speed = row.get("speed_download")
        mbps = ""
        if speed is not None:
            mbps = f"{speed * 8 / 1_000_000:.2f}"
        item = {
            "server": str(row["server_id"]),
            "iface": str(row["interface"]),
            "url": str(row["url"])[:42],
            "rc": str(row.get("rc") or ""),
            "http": str(row.get("http_code") or ""),
            "total_ms": str(row.get("time_total_ms") or ""),
            "ttfb_ms": str(row.get("time_starttransfer_ms") or ""),
            "mbps": mbps,
            "egress_ip": str(row.get("egress_ip") or ""),
            "remote_ip": str(row.get("remote_ip") or ""),
        }
        rendered.append(item)
        for key, value in item.items():
            widths[key] = max(widths[key], len(value))
    print("  ".join(header.ljust(widths[header]) for header in headers))
    print("  ".join("-" * widths[header] for header in headers))
    for item in rendered:
        print("  ".join(item[header].ljust(widths[header]) for header in headers))


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure Cudy route exits with curl --interface.")
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--servers", help="Comma-separated server ids or raw interfaces. Default: common exits.")
    parser.add_argument("--urls", help="Comma-separated URLs. Defaults include ifconfig, speedtest.net, web.telegram.org.")
    parser.add_argument("--url", action="append", dest="url_items", help="Add one URL to test. Can be repeated.")
    parser.add_argument("--download-url", help="Optional large-file URL for rough throughput measurement.")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--connect-timeout", type=int, default=5)
    parser.add_argument("--max-time", type=int, default=15)
    parser.add_argument("--ssh-host", default=DEFAULT_CUDY_HOST)
    parser.add_argument("--ssh-user", default=DEFAULT_CUDY_USER)
    parser.add_argument("--ssh-password")
    parser.add_argument("--ssh-timeout", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--csv", type=Path, help="Write results to CSV.")
    args = parser.parse_args()

    inventory = load_json(args.inventory)
    targets = resolve_targets(inventory, parse_csv_arg(args.servers))
    urls = parse_csv_arg(args.urls) or list(DEFAULT_URLS)
    if args.url_items:
        urls.extend(args.url_items)
    if args.download_url:
        urls.append(args.download_url)

    password = load_cudy_ssh_password(args.ssh_password)
    if not password:
        password = getpass.getpass(f"SSH password for {args.ssh_user}@{args.ssh_host}: ")

    results: list[dict[str, Any]] = []
    client = ssh_connect(args.ssh_host, args.ssh_user, password, args.ssh_timeout)
    try:
        ip_cache: dict[str, str] = {}
        for target in targets:
            if target.interface not in ip_cache:
                ip_cache[target.interface] = egress_ip(
                    client,
                    iface=target.interface,
                    connect_timeout=args.connect_timeout,
                    max_time=args.max_time,
                    timeout=args.ssh_timeout,
                )
            for url in urls:
                for iteration in range(1, max(1, args.repeat) + 1):
                    probe = curl_probe(
                        client,
                        iface=target.interface,
                        url=url,
                        connect_timeout=args.connect_timeout,
                        max_time=args.max_time,
                        timeout=args.ssh_timeout,
                    )
                    results.append(
                        {
                            "checked_at": utc_now(),
                            "server_id": target.server_id,
                            "label": target.label,
                            "provider": target.provider,
                            "kind": target.kind,
                            "interface": target.interface,
                            "egress_ip": ip_cache[target.interface],
                            "url": url,
                            "iteration": iteration,
                            "rc": to_int(probe.get("rc")),
                            "http_code": to_int(probe.get("http_code")),
                            "time_namelookup_ms": to_float_ms(probe.get("time_namelookup")),
                            "time_connect_ms": to_float_ms(probe.get("time_connect")),
                            "time_starttransfer_ms": to_float_ms(probe.get("time_starttransfer")),
                            "time_total_ms": to_float_ms(probe.get("time_total")),
                            "remote_ip": probe.get("remote_ip") or "",
                            "size_download": to_int(probe.get("size_download")),
                            "speed_download": to_int(probe.get("speed_download")),
                            "raw": probe.get("raw") or "",
                        }
                    )
    finally:
        client.close()

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(results[0].keys()) if results else ["checked_at"])
            writer.writeheader()
            writer.writerows(results)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        render_table(results)
        if args.csv:
            print(f"\nCSV: {args.csv}")

    failures = [row for row in results if row.get("rc") not in (0, None)]
    return 1 if failures and len(failures) == len(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
