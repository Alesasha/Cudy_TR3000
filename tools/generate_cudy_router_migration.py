#!/usr/bin/env python3
"""Generate a dry-run AirTies -> Cudy/OpenWrt migration plan.

The tool reads ignored AirTies/Cudy snapshots from backups/ and writes an
ignored markdown + shell plan. It never connects to routers and never applies
changes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKUPS = ROOT / "backups"


def latest_snapshot(base: Path) -> Path:
    candidates = [p for p in base.iterdir() if p.is_dir()]
    if not candidates:
        raise SystemExit(f"No snapshots found under {base}")
    return sorted(candidates)[-1]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def record_map(records: list[dict[str, str]]) -> dict[tuple[str, str], str]:
    return {(r.get("inst", ""), r.get("key", "")): r.get("value", "") for r in records}


def value(values: dict[tuple[str, str], str], inst: str, key: str, default: str = "") -> str:
    return values.get((inst, key), default)


def dhcp_reservations(values: dict[tuple[str, str], str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for idx in range(256):
        mac = value(values, "dhcps-0", f"mac{idx}")
        ip = value(values, "dhcps-0", f"ip{idx}")
        name = value(values, "dhcps-0", f"hostname{idx}")
        if mac or ip or name:
            rows.append({"idx": str(idx), "mac": mac, "ip": ip, "name": name})
    return rows


def sh_quote(value_: str) -> str:
    return "'" + value_.replace("'", "'\"'\"'") + "'"


def uci_set(package: str, section: str, option: str, value_: str) -> str:
    return f"uci set {package}.{section}.{option}={sh_quote(value_)}"


def uci_add_list(package: str, section: str, option: str, value_: str) -> str:
    return f"uci add_list {package}.{section}.{option}={sh_quote(value_)}"


def sanitize_section_name(prefix: str, value_: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "_" for ch in value_)
    safe = "_".join(part for part in safe.split("_") if part)
    return f"{prefix}_{safe[:48]}".rstrip("_")


def generate_commands(
    values: dict[tuple[str, str], str],
    nat_rows: list[dict[str, str]],
    *,
    include_disabled_forwards: bool,
) -> list[str]:
    wan_ip = value(values, "static-1", "settings.ip")
    wan_mask = value(values, "static-1", "settings.mask")
    wan_gateway = value(values, "static-1", "settings.gateway")
    wan_dns1 = value(values, "static-1", "settings.dns1")
    wan_dns2 = value(values, "static-1", "settings.dns2")
    wan_vlan = value(values, "wan_vlan-1", "vlanid")
    lan_ip = value(values, "static-0", "settings.ip")
    lan_mask = value(values, "static-0", "settings.mask")
    dhcp_start_ip = value(values, "dhcps-0", "startip")
    dhcp_end_ip = value(values, "dhcps-0", "endip")
    dhcp_lease_seconds = value(values, "dhcps-0", "leasetime")

    start_octet = int(dhcp_start_ip.rsplit(".", 1)[1]) if dhcp_start_ip else 10
    end_octet = int(dhcp_end_ip.rsplit(".", 1)[1]) if dhcp_end_ip else 249
    dhcp_limit = max(0, end_octet - start_octet + 1)

    commands = [
        "#!/bin/sh",
        "set -eu",
        "",
        "echo 'DRY-RUN MIGRATION SCRIPT: review before applying.'",
        "echo 'This script intentionally exits before changing OpenWrt.'",
        "exit 1",
        "",
        "# Backup first:",
        "sysupgrade -b /tmp/cudy-before-airties-migration.tar.gz",
        "",
        "# WAN: AirTies used VLAN ID 2. Review the correct OpenWrt device syntax",
        "# for this Cudy build before applying. Current snapshot used WAN device eth0.",
        f"# Suggested VLAN device name if supported: eth0.{wan_vlan}",
        "# uci add network device",
        "# uci set network.@device[-1].type='8021q'",
        "# uci set network.@device[-1].ifname='eth0'",
        f"# uci set network.@device[-1].vid={sh_quote(wan_vlan)}",
        f"# uci set network.@device[-1].name={sh_quote('eth0.' + wan_vlan)}",
        "",
        "# WAN static settings:",
        "uci set network.wan.proto='static'",
        f"# Review VLAN first: uci set network.wan.device={sh_quote('eth0.' + wan_vlan)}",
        uci_set("network", "wan", "ipaddr", wan_ip),
        uci_set("network", "wan", "netmask", wan_mask),
        uci_set("network", "wan", "gateway", wan_gateway),
        "uci -q delete network.wan.dns || true",
        uci_add_list("network", "wan", "dns", wan_dns1),
        uci_add_list("network", "wan", "dns", wan_dns2),
        "",
        "# LAN cutover. This will move Cudy management from the current subnet.",
        uci_set("network", "lan", "ipaddr", lan_ip),
        uci_set("network", "lan", "netmask", lan_mask),
        "",
        "# DHCP pool translated from AirTies start/end to OpenWrt start/limit.",
        uci_set("dhcp", "lan", "start", str(start_octet)),
        uci_set("dhcp", "lan", "limit", str(dhcp_limit)),
        uci_set("dhcp", "lan", "leasetime", f"{dhcp_lease_seconds}s"),
        "",
        "# DHCP reservations from AirTies.",
    ]

    for row in dhcp_reservations(values):
        if row["name"].lower() == "openwrt" and row["ip"] == "192.168.1.174":
            commands.extend(
                [
                    "# skipped old AirTies-side reservation for Cudy/OpenWrt WAN",
                    "# old mapping: OpenWrt -> 192.168.1.174",
                    "",
                ]
            )
            continue
        section = sanitize_section_name("airties_host", row["name"] or row["ip"] or row["mac"])
        commands.extend(
            [
                f"uci -q delete dhcp.{section} || true",
                f"uci set dhcp.{section}='host'",
                uci_set("dhcp", section, "name", row["name"]),
                uci_set("dhcp", section, "mac", row["mac"].lower()),
                uci_set("dhcp", section, "ip", row["ip"]),
                "",
            ]
        )

    commands.extend(
        [
            "# Keep the existing Cudy AWG WAN allow rule for UDP 51830.",
            "# Do not create a redirect to Cudy itself after Cudy is the border router.",
            "",
            "# Port forwards migrated from AirTies. Disabled AirTies forwards are",
            "# commented unless --include-disabled-forwards was used.",
        ]
    )

    for row in nat_rows:
        active = row.get("active") == "1"
        if not active and not include_disabled_forwards:
            commands.append(
                f"# skipped disabled AirTies forward {row.get('id')}: {row.get('name')} -> {row.get('client_ip')}"
            )
            continue
        if row.get("client_ip") == lan_ip:
            commands.append(
                "# skipped AirTies router self-forward "
                f"{row.get('id')}: {row.get('name')} {row.get('tcp_wan') or row.get('udp_wan')}"
            )
            continue
        if row.get("name") == "Cudy" and row.get("udp_wan") == "51830":
            commands.append("# skipped AirTies self-forward for Cudy UDP 51830; keep WAN allow rule instead")
            continue
        section = sanitize_section_name("airties_redirect", f"{row.get('id')}_{row.get('name')}")
        prefix = "" if active else "# disabled on AirTies: "
        commands.extend(
            [
                f"{prefix}uci -q delete firewall.{section} || true",
                f"{prefix}uci set firewall.{section}='redirect'",
                f"{prefix}{uci_set('firewall', section, 'name', row.get('name', ''))}",
                f"{prefix}uci set firewall.{section}.src='wan'",
                f"{prefix}uci set firewall.{section}.dest='lan'",
                f"{prefix}{uci_set('firewall', section, 'dest_ip', row.get('client_ip', ''))}",
                f"{prefix}uci set firewall.{section}.target='DNAT'",
            ]
        )
        protos: list[str] = []
        if row.get("tcp_wan"):
            protos.append("tcp")
            commands.extend(
                [
                    f"{prefix}{uci_set('firewall', section, 'src_dport', row['tcp_wan'])}",
                    f"{prefix}{uci_set('firewall', section, 'dest_port', row['tcp_lan'])}",
                ]
            )
        if row.get("udp_wan"):
            protos.append("udp")
            commands.extend(
                [
                    f"{prefix}{uci_set('firewall', section, 'src_dport', row['udp_wan'])}",
                    f"{prefix}{uci_set('firewall', section, 'dest_port', row['udp_lan'])}",
                ]
            )
        if protos:
            commands.append(f"{prefix}{uci_set('firewall', section, 'proto', ' '.join(protos))}")
        commands.append("")

    commands.extend(
        [
            "# Security hardening recommended during migration:",
            "# - keep firewall defaults enabled on Cudy",
            "# - do not enable telnet",
            "# - do not expose LuCI/uhttpd on WAN unless strictly needed",
            "# - keep UPnP/WPS/TR-069 disabled unless explicitly required",
            "",
            "# Final apply sequence, only after review:",
            "uci commit network",
            "uci commit dhcp",
            "uci commit firewall",
            "/etc/init.d/network reload",
            "/etc/init.d/dnsmasq restart",
            "/etc/init.d/firewall restart",
        ]
    )
    return commands


def render_markdown(
    airties_snapshot: Path,
    cudy_snapshot: Path | None,
    values: dict[tuple[str, str], str],
    nat_rows: list[dict[str, str]],
    command_path: Path,
) -> str:
    reservations = dhcp_reservations(values)
    active_forwards = [r for r in nat_rows if r.get("active") == "1"]
    disabled_forwards = [r for r in nat_rows if r.get("active") != "1"]

    lines = [
        "# Generated AirTies -> Cudy Migration Plan",
        "",
        f"- Generated: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- AirTies snapshot: `{airties_snapshot}`",
    ]
    if cudy_snapshot:
        lines.append(f"- Cudy snapshot: `{cudy_snapshot}`")
    lines.extend(
        [
            f"- Dry-run shell plan: `{command_path}`",
            "",
            "## WAN",
            "",
            f"- Static IP: `{value(values, 'static-1', 'settings.ip')}`",
            f"- Netmask: `{value(values, 'static-1', 'settings.mask')}`",
            f"- Gateway: `{value(values, 'static-1', 'settings.gateway')}`",
            f"- DNS: `{value(values, 'static-1', 'settings.dns1')}`, `{value(values, 'static-1', 'settings.dns2')}`",
            f"- VLAN ID: `{value(values, 'wan_vlan-1', 'vlanid')}`",
            "- VLAN syntax is review-required on Cudy/OpenWrt before applying.",
            "",
            "## LAN/DHCP",
            "",
            f"- LAN IP: `{value(values, 'static-0', 'settings.ip')}`",
            f"- LAN mask: `{value(values, 'static-0', 'settings.mask')}`",
            f"- DHCP range: `{value(values, 'dhcps-0', 'startip')}` - `{value(values, 'dhcps-0', 'endip')}`",
            f"- DHCP lease: `{value(values, 'dhcps-0', 'leasetime')}` seconds",
            f"- Static reservations: `{len(reservations)}`",
            "",
            "## Port Forwards",
            "",
            f"- Active forwards: `{len(active_forwards)}`",
            f"- Disabled forwards preserved as comments: `{len(disabled_forwards)}`",
            "",
            "| Active | Name | Client IP | TCP WAN -> LAN | UDP WAN -> LAN |",
            "|---|---|---|---|---|",
        ]
    )
    for row in nat_rows:
        tcp = f"{row.get('tcp_wan')} -> {row.get('tcp_lan')}" if row.get("tcp_wan") else ""
        udp = f"{row.get('udp_wan')} -> {row.get('udp_lan')}" if row.get("udp_wan") else ""
        lines.append(
            f"| {row.get('active')} | `{row.get('name')}` | `{row.get('client_ip')}` | `{tcp}` | `{udp}` |"
        )

    lines.extend(
        [
            "",
            "## Apply Guardrails",
            "",
            "- The generated shell script exits before changing anything.",
            "- Review WAN VLAN handling before removing the guard.",
            "- Apply only while physically ready to move the ISP uplink to Cudy.",
            "- Keep AirTies available for rollback.",
            "- Re-test Cudy AWG, control-server fallback, provider transports, and direct LAN traffic after cutover.",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--airties-snapshot",
        type=Path,
        default=None,
        help="Path to backups/airties/snapshots/<stamp>. Defaults to latest.",
    )
    parser.add_argument(
        "--cudy-snapshot",
        type=Path,
        default=None,
        help="Path to backups/cudy/snapshots/<stamp>. Defaults to latest if present.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory. Defaults to the AirTies snapshot directory.",
    )
    parser.add_argument(
        "--include-disabled-forwards",
        action="store_true",
        help="Emit disabled AirTies forwards as commented UCI commands.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    airties_snapshot = args.airties_snapshot or latest_snapshot(BACKUPS / "airties" / "snapshots")
    cudy_base = BACKUPS / "cudy" / "snapshots"
    cudy_snapshot = args.cudy_snapshot
    if cudy_snapshot is None and cudy_base.exists():
        cudy_snapshot = latest_snapshot(cudy_base)

    records_path = airties_snapshot / "records.json"
    nat_path = airties_snapshot / "nat_port_forwarding_table.json"
    if not records_path.exists() or not nat_path.exists():
        raise SystemExit(
            f"Snapshot {airties_snapshot} must contain records.json and nat_port_forwarding_table.json"
        )

    output_dir = args.output_dir or airties_snapshot
    output_dir.mkdir(parents=True, exist_ok=True)

    records = load_json(records_path)
    nat_rows = load_json(nat_path)
    values = record_map(records)

    sh_path = output_dir / "cudy-airties-migration-dry-run.sh"
    md_path = output_dir / "cudy-airties-migration-plan.md"

    sh_path.write_text(
        "\n".join(
            generate_commands(
                values,
                nat_rows,
                include_disabled_forwards=args.include_disabled_forwards,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    md_path.write_text(
        render_markdown(airties_snapshot, cudy_snapshot, values, nat_rows, sh_path),
        encoding="utf-8",
    )

    print(f"Wrote {md_path}")
    print(f"Wrote {sh_path}")
    print("Dry-run only: generated commands are guarded and are not applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
