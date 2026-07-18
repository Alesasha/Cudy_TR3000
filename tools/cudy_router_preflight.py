#!/usr/bin/env python3
"""Preflight checks for making Cudy the ISP-facing router.

The checks are intentionally offline: they read local snapshots under backups/
and report migration risks without connecting to routers or changing state.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from generate_cudy_router_migration import BACKUPS, dhcp_reservations, latest_snapshot, load_json, record_map, value


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


def parse_uci_show(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("$ ") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        values[key.strip()] = raw.strip().strip("'")
    return values


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def cudy_uci(cudy_snapshot: Path, name: str) -> dict[str, str]:
    return parse_uci_show(read_text(cudy_snapshot / f"uci_{name}.txt"))


def snapshot_generated_at(snapshot: Path) -> dt.datetime | None:
    index_path = snapshot / "index.json"
    if index_path.exists():
        try:
            index = load_json(index_path)
            if isinstance(index, dict) and index.get("generated_at"):
                parsed = dt.datetime.fromisoformat(str(index["generated_at"]).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                return parsed.astimezone(dt.timezone.utc)
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
    match = re.fullmatch(r"(\d{8})-(\d{6})", snapshot.name)
    if not match:
        return None
    parsed = dt.datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
    return parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo).astimezone(dt.timezone.utc)


def active_forward_targets(nat_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [row for row in nat_rows if row.get("active") == "1"]


def reservation_ips(values: dict[tuple[str, str], str]) -> set[str]:
    return {row["ip"] for row in dhcp_reservations(values) if row.get("ip")}


def cudy_firewall_has_awg_allow(firewall: dict[str, str], port: str) -> bool:
    sections = sorted({key.rsplit(".", 1)[0] for key in firewall})
    for section in sections:
        if firewall.get(f"{section}.dest_port") != port:
            continue
        if firewall.get(f"{section}.src") != "wan":
            continue
        if firewall.get(f"{section}.target") == "ACCEPT":
            return True
    return False


def cudy_awg_listen_ports(network: dict[str, str]) -> set[str]:
    ports: set[str] = set()
    for key, val in network.items():
        if key.endswith(".listen_port"):
            ports.add(val)
    return ports


def cudy_wifi_summary(wireless: dict[str, str]) -> tuple[int, int, list[str]]:
    sections = sorted(
        key
        for key, val in wireless.items()
        if val == "wifi-iface" and key.count(".") >= 1
    )
    enabled = 0
    encrypted = 0
    ssids: list[str] = []
    for section in sections:
        disabled = wireless.get(f"{section}.disabled", "0") == "1"
        encryption = wireless.get(f"{section}.encryption", "none").lower()
        if not disabled:
            enabled += 1
            if encryption not in {"", "none", "open"}:
                encrypted += 1
        ssid = wireless.get(f"{section}.ssid", "<unnamed>")
        ssids.append(f"{ssid}:{'disabled' if disabled else encryption}")
    return enabled, encrypted, ssids


def host_routes_via_airties(cudy_snapshot: Path, airties_lan_ip: str) -> list[str]:
    routes = []
    route_text = read_text(cudy_snapshot / "ip_route.txt")
    pattern = re.compile(r"^\S+ via " + re.escape(airties_lan_ip) + r" dev \S+.*$", re.MULTILINE)
    for match in pattern.finditer(route_text):
        line = match.group(0)
        if line.startswith("default "):
            continue
        routes.append(line)
    return routes


def run_preflight(
    airties_snapshot: Path,
    cudy_snapshot: Path,
    *,
    now: dt.datetime | None = None,
) -> list[Finding]:
    records = load_json(airties_snapshot / "records.json")
    nat_rows = load_json(airties_snapshot / "nat_port_forwarding_table.json")
    values = record_map(records)
    cudy_network = cudy_uci(cudy_snapshot, "network")
    cudy_dhcp = cudy_uci(cudy_snapshot, "dhcp")
    cudy_firewall = cudy_uci(cudy_snapshot, "firewall")
    cudy_wireless = cudy_uci(cudy_snapshot, "wireless")

    findings: list[Finding] = []

    generated_at = snapshot_generated_at(cudy_snapshot)
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    if generated_at is None:
        findings.append(
            Finding("WARN", "cudy-snapshot-age-unknown", "Cudy snapshot timestamp could not be verified.")
        )
    else:
        age_hours = max(0.0, (current.astimezone(dt.timezone.utc) - generated_at).total_seconds() / 3600)
        if age_hours <= 24:
            findings.append(
                Finding("PASS", "cudy-snapshot-fresh", "Cudy snapshot is fresh.", f"age={age_hours:.1f}h")
            )
        else:
            findings.append(
                Finding(
                    "WARN",
                    "cudy-snapshot-stale",
                    "Cudy snapshot is older than 24 hours; capture a fresh snapshot before cutover.",
                    f"age={age_hours:.1f}h",
                )
            )

    airties_wan_vlan = value(values, "wan_vlan-1", "vlanid")
    cudy_wan_device = cudy_network.get("network.wan.device", "")
    if airties_wan_vlan:
        if cudy_wan_device.endswith(f".{airties_wan_vlan}"):
            findings.append(Finding("PASS", "wan-vlan", "Cudy WAN already references the AirTies VLAN."))
        else:
            findings.append(
                Finding(
                    "WARN",
                    "wan-vlan-review",
                    "AirTies WAN uses VLAN; Cudy current WAN device does not show that VLAN.",
                    f"AirTies VLAN={airties_wan_vlan}, Cudy WAN device={cudy_wan_device or '<missing>'}",
                )
            )

    airties_wan_ip = value(values, "static-1", "settings.ip")
    cudy_wan_proto = cudy_network.get("network.wan.proto", "")
    if cudy_wan_proto != "static":
        findings.append(
            Finding(
                "INFO",
                "wan-static-cutover",
                "Cudy WAN is not yet configured with AirTies static public IP.",
                f"AirTies public IP={airties_wan_ip}, Cudy proto={cudy_wan_proto}",
            )
        )

    airties_lan_ip = value(values, "static-0", "settings.ip")
    cudy_lan_ip = cudy_network.get("network.lan.ipaddr", "")
    if cudy_lan_ip != airties_lan_ip:
        findings.append(
            Finding(
                "WARN",
                "lan-ip-cutover",
                "Cudy LAN IP differs from AirTies LAN IP; cutover will move management address.",
                f"AirTies LAN={airties_lan_ip}, Cudy LAN={cudy_lan_ip}",
            )
        )

    dhcp_start_ip = value(values, "dhcps-0", "startip")
    dhcp_end_ip = value(values, "dhcps-0", "endip")
    cudy_dhcp_start = cudy_dhcp.get("dhcp.lan.start", "")
    cudy_dhcp_limit = cudy_dhcp.get("dhcp.lan.limit", "")
    findings.append(
        Finding(
            "INFO",
            "dhcp-cutover",
            "AirTies DHCP range must replace current Cudy DHCP range at cutover.",
            f"AirTies={dhcp_start_ip}-{dhcp_end_ip}; Cudy start={cudy_dhcp_start}, limit={cudy_dhcp_limit}",
        )
    )

    wifi_enabled, wifi_encrypted, wifi_ssids = cudy_wifi_summary(cudy_wireless)
    if wifi_enabled == 0:
        findings.append(
            Finding(
                "WARN",
                "wifi-disabled",
                "Cudy has no enabled Wi-Fi interface; configure and test Wi-Fi before replacing AirTies.",
                ", ".join(wifi_ssids) or "no wifi-iface sections",
            )
        )
    elif wifi_encrypted < wifi_enabled:
        findings.append(
            Finding(
                "WARN",
                "wifi-unencrypted",
                "At least one enabled Cudy Wi-Fi interface is not encrypted.",
                ", ".join(wifi_ssids),
            )
        )
    else:
        findings.append(
            Finding(
                "PASS",
                "wifi-ready",
                "Cudy has at least one enabled encrypted Wi-Fi interface.",
                ", ".join(wifi_ssids),
            )
        )

    port = value(values, "network.awg_in", "listen_port", "51830")
    # AirTies records do not contain Cudy network, so use known Cudy port from cudy snapshot.
    awg_ports = cudy_awg_listen_ports(cudy_network)
    if "51830" in awg_ports:
        findings.append(Finding("PASS", "awg-listen-port", "Cudy AWG listener includes UDP 51830."))
    else:
        findings.append(
            Finding(
                "FAIL",
                "awg-listen-port-missing",
                "Cudy AWG listener port 51830 was not found in the Cudy network snapshot.",
                f"found={', '.join(sorted(awg_ports)) or '<none>'}",
            )
        )
    if cudy_firewall_has_awg_allow(cudy_firewall, "51830"):
        findings.append(Finding("PASS", "awg-wan-allow", "Cudy firewall has a WAN allow rule for UDP 51830."))
    else:
        findings.append(Finding("FAIL", "awg-wan-allow-missing", "Cudy firewall lacks a WAN allow rule for UDP 51830."))

    reserved_ips = reservation_ips(values)
    missing_reservations = []
    self_forwards = []
    cudy_self_forwards = []
    for row in active_forward_targets(nat_rows):
        target = row.get("client_ip", "")
        if target == airties_lan_ip:
            self_forwards.append(row)
            continue
        if row.get("name") == "Cudy" and row.get("udp_wan") == "51830":
            cudy_self_forwards.append(row)
            continue
        if target and target not in reserved_ips:
            missing_reservations.append(row)

    if self_forwards:
        findings.append(
            Finding(
                "INFO",
                "airties-self-forwards",
                "AirTies had self-forwards to its own LAN IP; these should not be recreated on Cudy.",
                ", ".join(f"{r.get('name')}:{r.get('tcp_wan') or r.get('udp_wan')}" for r in self_forwards),
            )
        )
    if cudy_self_forwards:
        findings.append(
            Finding(
                "INFO",
                "cudy-old-forward",
                "AirTies forwarded UDP 51830 to Cudy; after cutover this becomes a direct WAN allow rule.",
                ", ".join(f"{r.get('client_ip')}:{r.get('udp_wan')}" for r in cudy_self_forwards),
            )
        )
    if missing_reservations:
        findings.append(
            Finding(
                "WARN",
                "forward-targets-without-reservations",
                "Some active forwarded targets do not have AirTies static DHCP reservations.",
                ", ".join(f"{r.get('name')}->{r.get('client_ip')}" for r in missing_reservations),
            )
        )
    else:
        findings.append(Finding("PASS", "forward-target-reservations", "Active forward targets have static reservations or are expected self-forwards."))

    old_cudy_reservation = any(
        row.get("name", "").lower() == "openwrt" and row.get("ip") == "192.168.1.174"
        for row in dhcp_reservations(values)
    )
    if old_cudy_reservation:
        findings.append(
            Finding(
                "INFO",
                "old-cudy-reservation",
                "AirTies has an OpenWrt reservation for Cudy's old behind-AirTies WAN address; skip it after cutover.",
                "OpenWrt -> 192.168.1.174",
            )
        )

    old_routes = host_routes_via_airties(cudy_snapshot, airties_lan_ip)
    if old_routes:
        findings.append(
            Finding(
                "WARN",
                "host-routes-via-old-airties",
                "Cudy has host routes via the current AirTies LAN gateway; review them after WAN cutover.",
                "; ".join(old_routes[:10]),
            )
        )

    remote_management = value(values, "security-0", "REM.enabled")
    upnp = value(values, "upnp-0", "enablePF")
    tr069 = value(values, "tr069-0", "tr069.enable")
    if remote_management == "1" or upnp == "1" or tr069 == "1":
        findings.append(
            Finding(
                "INFO",
                "airties-management-services",
                "AirTies had remote management/UPnP/TR-069 enabled; prefer not enabling these on Cudy.",
                f"remote={remote_management}, upnp={upnp}, tr069={tr069}",
            )
        )

    return findings


def render_markdown(airties_snapshot: Path, cudy_snapshot: Path, findings: list[Finding]) -> str:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1

    lines = [
        "# Cudy Main Router Preflight",
        "",
        f"- Generated: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- AirTies snapshot: `{airties_snapshot}`",
        f"- Cudy snapshot: `{cudy_snapshot}`",
        f"- Summary: PASS={counts.get('PASS', 0)}, WARN={counts.get('WARN', 0)}, FAIL={counts.get('FAIL', 0)}, INFO={counts.get('INFO', 0)}",
        "",
        "| Severity | Code | Message | Detail |",
        "|---|---|---|---|",
    ]
    for finding in findings:
        detail = finding.detail.replace("|", "\\|")
        lines.append(
            f"| {finding.severity} | `{finding.code}` | {finding.message} | {detail} |"
        )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--airties-snapshot", type=Path, default=None)
    parser.add_argument("--cudy-snapshot", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="Print findings as JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    airties_snapshot = args.airties_snapshot or latest_snapshot(BACKUPS / "airties" / "snapshots")
    cudy_snapshot = args.cudy_snapshot or latest_snapshot(BACKUPS / "cudy" / "snapshots")
    output_dir = args.output_dir or airties_snapshot
    output_dir.mkdir(parents=True, exist_ok=True)

    findings = run_preflight(airties_snapshot, cudy_snapshot)
    json_path = output_dir / "cudy-main-router-preflight.json"
    md_path = output_dir / "cudy-main-router-preflight.md"
    json_path.write_text(
        json.dumps([finding.as_dict() for finding in findings], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(airties_snapshot, cudy_snapshot, findings), encoding="utf-8")

    if args.json:
        print(json.dumps([finding.as_dict() for finding in findings], ensure_ascii=False, indent=2))
    else:
        for finding in findings:
            suffix = f" - {finding.detail}" if finding.detail else ""
            print(f"{finding.severity} {finding.code}: {finding.message}{suffix}")
        print(f"Wrote {md_path}")
        print(f"Wrote {json_path}")
    return 1 if any(f.severity == "FAIL" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
