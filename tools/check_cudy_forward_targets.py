#!/usr/bin/env python3
"""Probe AirTies port-forward targets from Cudy without changing configuration."""

from __future__ import annotations

import argparse
import ipaddress
import json
import shlex
from pathlib import Path
from typing import Any

from deploy_cudy_go_fallback import connect, load_password, ssh_exec
from generate_cudy_router_migration import BACKUPS, dhcp_reservations, latest_snapshot, load_json, record_map, value


def collect_targets(snapshot: Path) -> list[dict[str, Any]]:
    records = load_json(snapshot / "records.json")
    rows = load_json(snapshot / "nat_port_forwarding_table.json")
    values = record_map(records)
    lan_ip = value(values, "static-0", "settings.ip")
    reservations = {item["ip"]: item for item in dhcp_reservations(values) if item.get("ip")}
    targets: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("active") != "1":
            continue
        target = str(row.get("client_ip") or "").strip()
        try:
            parsed = ipaddress.ip_address(target)
        except ValueError:
            continue
        if parsed.version != 4 or target == lan_ip:
            continue
        if row.get("name") == "Cudy" and row.get("udp_wan") == "51830":
            continue
        item = targets.setdefault(
            target,
            {
                "target": target,
                "reservation": reservations.get(target),
                "forwards": [],
            },
        )
        item["forwards"].append(
            {
                "name": row.get("name") or "",
                "tcp": f"{row.get('tcp_wan')}->{row.get('tcp_lan')}" if row.get("tcp_wan") else "",
                "udp": f"{row.get('udp_wan')}->{row.get('udp_lan')}" if row.get("udp_wan") else "",
            }
        )
    return [targets[key] for key in sorted(targets, key=ipaddress.ip_address)]


def tcp_ports(item: dict[str, Any]) -> list[int]:
    result: set[int] = set()
    for forward in item.get("forwards") or []:
        value = str(forward.get("tcp") or "")
        if "->" not in value:
            continue
        try:
            port = int(value.split("->", 1)[1])
        except ValueError:
            continue
        if 1 <= port <= 65535:
            result.add(port)
    return sorted(result)


def probe(client: Any, item: dict[str, Any], timeout: int) -> dict[str, Any]:
    target = str(item["target"])
    validated = str(ipaddress.ip_address(target))
    quoted = shlex.quote(validated)
    port_checks = " ".join(
        f"nc -z -w 2 {quoted} {port} >/dev/null 2>&1; printf 'tcp_{port}_rc=%s\\n' $?;"
        for port in tcp_ports(item)
    )
    _rc, output = ssh_exec(
        client,
        (
            f"printf 'route='; ip -4 route get {quoted} 2>&1 | head -1 || true; printf '\n'; "
            f"ping -c 2 -W 2 {quoted} >/dev/null 2>&1; printf 'ping_rc=%s\n' $?; "
            f"printf 'neighbor='; ip -4 neigh show to {quoted} 2>&1 | head -1 || true; printf '\n'; "
            f"{port_checks}"
        ),
        timeout,
    )
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, val = line.split("=", 1)
            fields[key] = val.strip()
    neighbor = fields.get("neighbor", "")
    ping_ok = fields.get("ping_rc") == "0"
    l2_present = "lladdr " in neighbor and " FAILED" not in neighbor
    open_tcp_ports = [port for port in tcp_ports(item) if fields.get(f"tcp_{port}_rc") == "0"]
    return {
        "route": fields.get("route", ""),
        "ping_ok": ping_ok,
        "neighbor": neighbor,
        "l2_present": l2_present,
        "open_tcp_ports": open_tcp_ports,
        "present": ping_ok or l2_present or bool(open_tcp_ports),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    snapshot = args.airties_snapshot or latest_snapshot(BACKUPS / "airties" / "snapshots")
    targets = collect_targets(snapshot)
    client = connect(args.host, args.user, load_password(args.ssh_password), args.timeout)
    try:
        for target in targets:
            target.update(probe(client, target, args.timeout))
    finally:
        client.close()
    result = {
        "ok": all(target["present"] for target in targets),
        "host": args.host,
        "airties_snapshot": str(snapshot),
        "targets": targets,
        "present": sum(1 for target in targets if target["present"]),
        "total": len(targets),
        "without_reservation": sum(1 for target in targets if not target["reservation"]),
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.8.1")
    parser.add_argument("--user", default="root")
    parser.add_argument("--ssh-password")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--airties-snapshot", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"Cudy forward targets: {'OK' if result['ok'] else 'WARN'} "
            f"present={result['present']}/{result['total']} "
            f"without_reservation={result['without_reservation']}"
        )
        for item in result["targets"]:
            names = ", ".join(sorted({forward["name"] for forward in item["forwards"]}))
            mac = ""
            if "lladdr " in item["neighbor"]:
                mac = item["neighbor"].split("lladdr ", 1)[1].split()[0]
            print(
                f"  {'OK' if item['present'] else 'MISSING'} {item['target']} "
                f"names={names or '-'} ping={item['ping_ok']} mac={mac or '-'} "
                f"tcp={','.join(str(port) for port in item['open_tcp_ports']) or '-'} "
                f"reserved={'yes' if item['reservation'] else 'no'}"
            )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
