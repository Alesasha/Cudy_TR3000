#!/usr/bin/env python3
"""
Stage 1 VPN inventory helper.

The static catalog lives in config/vpn_inventory.json. Runtime state from Cudy
is collected into config/cudy-runtime.json and can later feed the admin UI.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paramiko


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "config" / "vpn_inventory.json"
DEFAULT_RUNTIME = ROOT / "config" / "cudy-runtime.json"
DEFAULT_CUDY_HOST = "192.168.8.1"
DEFAULT_CUDY_USER = "root"
DEFAULT_CUDY_PASSWORD_FILE = ROOT / "secrets" / "cudy_ssh_password.txt"

REMOTE_REFRESH_MARKER = "@@VPN_PROVIDER_REFRESH_COMMAND"


REMOTE_SCRIPT = r"""#!/bin/sh
section() {
  printf '\n@@VPN_INVENTORY_SECTION:%s@@\n' "$1"
}

run_limited() {
  seconds="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$seconds" "$@" </dev/null
  else
    "$@" </dev/null
  fi
}

section supported_interfaces
uci -q get pbr.config.supported_interface 2>/dev/null || true

section target_interface
sed -n "s/^TARGET_INTERFACE='\([^']*\)'.*/\1/p" /usr/share/pbr/pbr.user.opencck-merged-vpn 2>/dev/null | tail -1 || true

section links
ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | sed 's/@.*//' || true

section ipv4
ip -4 -o addr show 2>/dev/null | awk '{print $2 "\t" $4}' || true

section service_status
for s in sing-box-vpntype sing-box-lokvpn \
  sing-box-proxygb sing-box-proxyca sing-box-proxyfr sing-box-proxyby \
  sing-box-proxyae sing-box-proxyhk sing-box-proxykz sing-box-proxytr \
  sing-box-proxyil sing-box-proxycz sing-box-proxypl sing-box-proxyfi \
  sing-box-proxynl sing-box-proxyal sing-box-proxyru sing-box-proxyus \
  sing-box-proxyde pbr; do
  if [ -x "/etc/init.d/$s" ]; then
    status="$(run_limited 3 /etc/init.d/$s status 2>/dev/null | head -1 || true)"
    printf '%s\t%s\n' "$s" "$status"
  else
    printf '%s\tmissing\n' "$s"
  fi
done

section vpntype_status
if [ -x /usr/bin/vpntype-server ]; then
  run_limited 5 /usr/bin/vpntype-server status 2>&1 || true
else
  echo missing
fi

section vpntype_list
if [ -x /usr/bin/vpntype-server ]; then
  run_limited 5 /usr/bin/vpntype-server list 2>&1 || true
else
  echo missing
fi

section lokvpn_profile
cat /etc/lokvpn-profile 2>/dev/null || true

section proxy_refreshers
for p in proxygb proxyca proxyfr proxyby proxyae proxyhk proxykz proxytr proxyil proxycz proxypl proxyfi proxynl proxyal proxyru proxyus proxyde; do
  if [ -x "/usr/bin/$p-refresh" ]; then
    printf '%s\tyes\n' "$p"
  else
    printf '%s\tno\n' "$p"
  fi
done

section switchers
for v in vpn1 vpn2 vpn3 vpn4 vpn-lokvpn vpn-vpntype vpn-switch \
  vpn-proxygb vpn-proxyca vpn-proxyfr vpn-proxyby vpn-proxyae \
  vpn-proxyhk vpn-proxykz vpn-proxytr vpn-proxyil vpn-proxycz \
  vpn-proxypl vpn-proxyfi vpn-proxynl vpn-proxyal vpn-proxyru \
  vpn-proxyus vpn-proxyde; do
  if command -v "$v" >/dev/null 2>&1; then
    printf '%s\tyes\n' "$v"
  else
    printf '%s\tno\n' "$v"
  fi
done

section pbr_sets
for ifname in awg1 awg2 awg4 vpntype lokvpn proxygb proxyca proxyfr proxyby proxyae proxyhk proxykz proxytr proxyil proxycz proxypl proxyfi proxynl proxyal proxyru proxyus proxyde; do
  nft list set inet fw4 "pbr_${ifname}_4_dst_ip_user" 2>/dev/null | awk -v i="$ifname" '/elements =/ {print i "\tpresent"}' || true
done
"""


SECTION_RE = re.compile(r"^@@VPN_INVENTORY_SECTION:([^@]+)@@$")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


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


def user_choices(inventory: dict[str, Any], *, include_disabled: bool = False) -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = []
    auto = inventory.get("auto_choice")
    if isinstance(auto, dict) and auto.get("user_visible") and (include_disabled or auto.get("enabled")):
        choices.append(
            {
                "id": auto.get("id", "auto"),
                "label": auto.get("label", "Auto"),
                "provider": "virtual",
                "interface": None,
                "geo": "auto",
                "kind": auto.get("kind", "virtual"),
            }
        )

    for server in inventory.get("servers", []):
        if not include_disabled and not server.get("enabled", False):
            continue
        if not server.get("user_visible", False):
            continue
        geo = server.get("geo") or {}
        country = geo.get("country", "")
        region = geo.get("region")
        geo_label = f"{country}-{region}" if region and country != "multi" else country
        choices.append(
            {
                "id": server["id"],
                "label": server.get("label", server["id"]),
                "provider": server.get("provider"),
                "interface": server.get("interface"),
                "geo": geo_label,
                "kind": server.get("kind"),
            }
        )
    return choices


def print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No rows.")
        return
    columns = ["id", "label", "provider", "interface", "geo", "kind"]
    widths = {col: max(len(col), *(len(str(row.get(col, ""))) for row in rows)) for col in columns}
    print("  ".join(col.ljust(widths[col]) for col in columns))
    print("  ".join("-" * widths[col] for col in columns))
    for row in rows:
        print("  ".join(str(row.get(col, "")).ljust(widths[col]) for col in columns))


def validate_inventory(inventory: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    ids: set[str] = set()
    interfaces: dict[str, str] = {}

    auto = inventory.get("auto_choice") or {}
    if auto.get("id") != "auto":
        errors.append("auto_choice.id must be 'auto'")

    for idx, server in enumerate(inventory.get("servers", [])):
        sid = server.get("id")
        if not sid:
            errors.append(f"servers[{idx}] is missing id")
            continue
        if sid in ids:
            errors.append(f"duplicate server id: {sid}")
        ids.add(sid)

        if server.get("enabled") and not server.get("interface"):
            errors.append(f"{sid}: enabled server is missing interface")

        iface = server.get("interface")
        if iface and server.get("enabled") and server.get("kind") != "sing-box-profile":
            previous = interfaces.setdefault(iface, sid)
            if previous != sid:
                errors.append(f"{sid}: interface {iface} is also used by {previous}")

        if server.get("kind") == "sing-box-selector" and server.get("provider") == "lokvpn":
            profiles = server.get("profiles") or []
            if not profiles:
                errors.append(f"{sid}: lokvpn selector has no profiles")

    return errors


def parse_sections(raw: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.splitlines():
        match = SECTION_RE.match(line.strip())
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def parse_tab_bool(text: str) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for line in text.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            result[parts[0]] = parts[1].strip().lower() == "yes"
    return result


def parse_service_status(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            result[parts[0]] = parts[1]
    return result


def parse_ipv4(text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            result.setdefault(parts[0], []).append(parts[1])
    return result


def parse_vpntype_current(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("current="):
            return line.split("=", 1)[1].strip() or None
    return None


def parse_vpntype_tags(text: str) -> list[dict[str, str]]:
    tags: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line.strip() or line.strip() == "missing":
            continue
        parts = line.split("\t")
        item = {"tag": parts[0]}
        if len(parts) > 1:
            item["type"] = parts[1]
        if len(parts) > 2:
            item["description"] = parts[2]
        tags.append(item)
    return tags


def build_provider_refresh_plan(
    inventory: dict[str, Any],
    *,
    target: str,
    profile: str | None = None,
) -> list[dict[str, Any]]:
    servers = {server.get("id"): server for server in inventory.get("servers", [])}
    target = target.strip().lower()
    plan: list[dict[str, Any]] = []

    def normalize_command(command: list[str]) -> list[str]:
        if not command:
            return command
        normalized = list(command)
        if "/" not in normalized[0]:
            normalized[0] = f"/usr/bin/{normalized[0]}"
        return normalized

    def add_command(provider: str, command: list[str], note: str) -> None:
        command = normalize_command(command)
        plan.append(
            {
                "provider": provider,
                "command": command,
                "shell": shlex.join(command),
                "note": note,
            }
        )

    if target in {"all", "vpntype"}:
        add_command(
            "vpntype",
            ["/usr/bin/vpntype-proxy-refresh-all"],
            "Refresh all VPNtype HTTP proxy endpoints using the existing Cudy script.",
        )

    if target in {"all", "lokvpn"}:
        command = ["/usr/bin/lokvpn-refresh-current"]
        if profile:
            command.append(profile)
        add_command(
            "lokvpn",
            command,
            "Refresh LokVPN sing-box config for the current or requested profile.",
        )

    if target not in {"all", "vpntype", "lokvpn"} and target in servers:
        server = servers[target]
        refresh_command = server.get("refresh_command") or server.get("profile_command")
        if not refresh_command:
            raise ValueError(f"{target}: no refresh_command/profile_command in inventory")
        add_command(
            str(server.get("provider") or target),
            shlex.split(str(refresh_command)),
            f"Refresh inventory server entry {target}.",
        )

    if not plan:
        raise ValueError("target must be all, vpntype, lokvpn, or a server id with refresh metadata")
    return plan


def ssh_run_refresh_plan(host: str, user: str, password: str, timeout: int, plan: list[dict[str, Any]]) -> str:
    lines = ["#!/bin/sh", "rc=0"]
    for idx, item in enumerate(plan, start=1):
        command = item["command"]
        shell = shlex.join(command)
        lines.append(f"printf '\\n{REMOTE_REFRESH_MARKER}:{idx}@@\\n'")
        lines.append(f"printf '%s\\n' {shlex.quote('$ ' + shell)}")
        lines.append(f"{shell} 2>&1")
        lines.append("code=$?")
        lines.append("if [ \"$code\" -ne 0 ]; then rc=1; fi")
        lines.append(f"printf '{REMOTE_REFRESH_MARKER}_RC:{idx}:%s@@\\n' \"$code\"")
    lines.append("exit \"$rc\"")
    script = "\n".join(lines) + "\n"

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
    try:
        remote_command = (
            'tmp="/tmp/vpn_provider_refresh_$$.sh"; '
            'cat > "$tmp"; sh "$tmp"; rc="$?"; rm -f "$tmp"; exit "$rc"'
        )
        stdin, stdout, stderr = client.exec_command(remote_command, timeout=timeout * max(3, len(plan) * 3))
        stdin.write(script)
        stdin.flush()
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        if err.strip():
            out += "\nSTDERR:\n" + err
        if rc:
            raise RuntimeError(f"remote provider refresh failed rc={rc}\n{out}")
        return out
    finally:
        client.close()


def build_runtime_snapshot(raw: str, *, host: str, user: str) -> dict[str, Any]:
    sections = parse_sections(raw)
    supported = sections.get("supported_interfaces", "").split()
    links = [line.strip() for line in sections.get("links", "").splitlines() if line.strip()]
    service_status = parse_service_status(sections.get("service_status", ""))
    proxy_refreshers = parse_tab_bool(sections.get("proxy_refreshers", ""))
    switchers = parse_tab_bool(sections.get("switchers", ""))

    return {
        "schema_version": 1,
        "collected_at": utc_now(),
        "host": host,
        "user": user,
        "supported_interfaces": supported,
        "target_interface": sections.get("target_interface", "").strip() or None,
        "links": links,
        "ipv4": parse_ipv4(sections.get("ipv4", "")),
        "service_status": service_status,
        "vpntype": {
            "current": parse_vpntype_current(sections.get("vpntype_status", "")),
            "status_raw": sections.get("vpntype_status", ""),
            "tags": parse_vpntype_tags(sections.get("vpntype_list", "")),
        },
        "lokvpn": {
            "profile": sections.get("lokvpn_profile", "").strip() or None,
            "service_status": service_status.get("sing-box-lokvpn"),
        },
        "proxy_refreshers": proxy_refreshers,
        "switchers": switchers,
        "pbr_sets_raw": sections.get("pbr_sets", ""),
        "raw_sections": sections,
    }


def ssh_collect(host: str, user: str, password: str, timeout: int) -> str:
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
    try:
        remote_command = (
            'tmp="/tmp/vpn_inventory_collect_$$.sh"; '
            'cat > "$tmp"; sh "$tmp"; rc="$?"; rm -f "$tmp"; exit "$rc"'
        )
        stdin, stdout, stderr = client.exec_command(remote_command, timeout=timeout * 3)
        stdin.write(REMOTE_SCRIPT)
        stdin.flush()
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        if rc:
            raise RuntimeError(f"remote inventory script failed rc={rc}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
        if err.strip():
            out += "\n@@VPN_INVENTORY_SECTION:stderr@@\n" + err
        return out
    finally:
        client.close()


def command_list(args: argparse.Namespace) -> int:
    inventory = read_json(args.inventory)
    rows = user_choices(inventory, include_disabled=args.include_disabled)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print_table(rows)
        print(f"\nUser-visible choices: {len(rows)}")
    return 0


def command_admin_list(args: argparse.Namespace) -> int:
    inventory = read_json(args.inventory)
    rows = []
    for server in inventory.get("servers", []):
        if not args.include_disabled and not server.get("enabled", False):
            continue
        if not server.get("admin_visible", False):
            continue
        geo = server.get("geo") or {}
        rows.append(
            {
                "id": server.get("id"),
                "label": server.get("label"),
                "provider": server.get("provider"),
                "interface": server.get("interface"),
                "geo": geo.get("country", ""),
                "kind": server.get("kind"),
            }
        )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print_table(rows)
        print(f"\nAdmin-visible servers: {len(rows)}")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    inventory = read_json(args.inventory)
    errors = validate_inventory(inventory)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"OK: {args.inventory}")
    return 0


def command_refresh_cudy(args: argparse.Namespace) -> int:
    password = load_cudy_ssh_password(args.ssh_password)
    if not password:
        password = getpass.getpass(f"SSH password for {args.ssh_user}@{args.ssh_host}: ")
    raw = ssh_collect(args.ssh_host, args.ssh_user, password, args.ssh_timeout)
    snapshot = build_runtime_snapshot(raw, host=args.ssh_host, user=args.ssh_user)
    write_json(args.output, snapshot)
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(f"Wrote runtime snapshot: {args.output}")
        print(f"Target interface: {snapshot.get('target_interface')}")
        print(f"Supported interfaces: {len(snapshot.get('supported_interfaces') or [])}")
        running = [name for name, value in snapshot.get("service_status", {}).items() if "running" in value]
        print(f"Running services: {', '.join(running) if running else 'none detected'}")
        print(f"LokVPN profile: {snapshot.get('lokvpn', {}).get('profile') or 'unknown'}")
        print(f"VPNtype current: {snapshot.get('vpntype', {}).get('current') or 'unknown'}")
    return 0


def command_refresh_provider(args: argparse.Namespace) -> int:
    inventory = read_json(args.inventory)
    try:
        plan = build_provider_refresh_plan(inventory, target=args.target, profile=args.profile)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    payload: dict[str, Any] = {
        "target": args.target,
        "apply": bool(args.apply),
        "ssh_host": args.ssh_host,
        "commands": plan,
    }

    if not args.apply:
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("Dry run. Use --apply to execute on Cudy.")
            for item in plan:
                print(f"{item['provider']}: {item['shell']}")
        return 0

    password = load_cudy_ssh_password(args.ssh_password)
    if not password:
        password = getpass.getpass(f"SSH password for {args.ssh_user}@{args.ssh_host}: ")

    try:
        output = ssh_run_refresh_plan(args.ssh_host, args.ssh_user, password, args.ssh_timeout, plan)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    payload["output"] = output
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(output.strip())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the local VPN/server inventory.")
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List user-visible routing choices.")
    list_parser.add_argument("--include-disabled", action="store_true")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=command_list)

    admin_parser = sub.add_parser("admin-list", help="List admin-visible servers.")
    admin_parser.add_argument("--include-disabled", action="store_true")
    admin_parser.add_argument("--json", action="store_true")
    admin_parser.set_defaults(func=command_admin_list)

    validate_parser = sub.add_parser("validate", help="Validate the static inventory file.")
    validate_parser.set_defaults(func=command_validate)

    refresh_parser = sub.add_parser("refresh-cudy", help="Collect runtime state from Cudy over SSH.")
    refresh_parser.add_argument("--ssh-host", default=DEFAULT_CUDY_HOST)
    refresh_parser.add_argument("--ssh-user", default=DEFAULT_CUDY_USER)
    refresh_parser.add_argument("--ssh-password")
    refresh_parser.add_argument("--ssh-timeout", type=int, default=20)
    refresh_parser.add_argument("--output", type=Path, default=DEFAULT_RUNTIME)
    refresh_parser.add_argument("--json", action="store_true")
    refresh_parser.set_defaults(func=command_refresh_cudy)

    provider_refresh_parser = sub.add_parser(
        "refresh-provider",
        help="Preview or run existing Cudy provider refresh scripts over SSH.",
    )
    provider_refresh_parser.add_argument(
        "target",
        nargs="?",
        default="all",
        help="all, vpntype, lokvpn, or a server id with refresh metadata.",
    )
    provider_refresh_parser.add_argument("--profile", help="LokVPN profile for lokvpn-refresh-current.")
    provider_refresh_parser.add_argument("--apply", action="store_true", help="Execute on Cudy. Default is dry-run.")
    provider_refresh_parser.add_argument("--ssh-host", default=DEFAULT_CUDY_HOST)
    provider_refresh_parser.add_argument("--ssh-user", default=DEFAULT_CUDY_USER)
    provider_refresh_parser.add_argument("--ssh-password")
    provider_refresh_parser.add_argument("--ssh-timeout", type=int, default=60)
    provider_refresh_parser.add_argument("--json", action="store_true")
    provider_refresh_parser.set_defaults(func=command_refresh_provider)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
