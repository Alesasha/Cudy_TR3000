#!/usr/bin/env python3
"""
Create an AmneziaWG client profile on an existing Docker-based AmneziaWG server
or on the Cudy inbound AmneziaWG server for remote friends.

Example:
  python tools/awg_client_add.py hostvds-uswest phone-alex
  python tools/awg_client_add.py cudy-home phone-alex
  python tools/awg_client_add.py all --stats

Password handling:
  $env:AWG_SSH_PASSWORD='...'
  python tools/awg_client_add.py hostvds-uswest phone-alex

or:
  python tools/awg_client_add.py hostvds-uswest phone-alex --ssh-password '...'

For all-server stats with different root passwords:
  $env:AWG_SSH_PASSWORD_HOSTVDS_USWEST='...'
  $env:AWG_SSH_PASSWORD_MEGAHOST_AKTAU='...'
  $env:AWG_SSH_PASSWORD_CUDY_HOME='...'
  python tools/awg_client_add.py all --stats
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

import paramiko


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "secrets" / "clients"
DEFAULT_CUDY_PASSWORD_FILE = ROOT / "secrets" / "cudy_ssh_password.txt"


SERVER_REGISTRY = {
    "hostvds-uswest": {
        "kind": "docker-awg",
        "ssh_host": "95.182.91.203",
        "ssh_user": "root",
        "endpoint_host": "95.182.91.203",
        "endpoint_port": 30184,
        "docker_container": "amnezia-awg2",
        "awg_interface": "awg0",
        "client_network": "10.8.1.0/24",
        "dns": "1.1.1.1",
        "mtu": 1420,
        "client_names": {
            "10.8.1.1/32": "Admin [Windows 11]",
            "10.8.1.2/32": "New client",
            "10.8.1.3/32": "New client",
            "10.8.1.4/32": "EmilyPC",
            "10.8.1.5/32": "New client",
            "10.8.1.6/32": "IosifTel",
            "10.8.1.7/32": "IraI",
            "10.8.1.8/32": "DC",
            "10.8.1.9/32": "DC",
            "10.8.1.10/32": "Cudy",
        },
    },
    "megahost-aktau": {
        "kind": "docker-awg",
        "ssh_host": "45.136.59.135",
        "ssh_user": "root",
        "endpoint_host": "45.136.59.135",
        "endpoint_port": 45646,
        "docker_container": "amnezia-awg2",
        "awg_interface": "awg0",
        "client_network": "10.8.1.0/24",
        "dns": "1.1.1.1",
        "mtu": 1420,
        "client_names": {
            "10.8.1.1/32": "Admin [Windows 11]",
            "10.8.1.2/32": "SashaMob",
            "10.8.1.3/32": "Emily",
            "10.8.1.4/32": "EmilyPC",
            "10.8.1.5/32": "IosifTel_K",
            "10.8.1.6/32": "IraI",
            "10.8.1.7/32": "DC",
            "10.8.1.8/32": "Cudy",
        },
    },
    "cudy-home": {
        "kind": "cudy-friends",
        "ssh_host": "192.168.8.1",
        "ssh_user": "root",
        "endpoint_host": "195.170.35.108",
        "endpoint_port": 51830,
        "client_network": "10.77.0.0/24",
        "dns": "10.77.0.1",
        "mtu": 1280,
        "friendctl": "/usr/bin/friendctl",
    },
}


KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class Server:
    name: str
    kind: str
    ssh_host: str
    ssh_user: str
    endpoint_host: str
    endpoint_port: int
    client_network: ipaddress.IPv4Network
    dns: str
    mtu: int
    docker_container: str = ""
    awg_interface: str = ""
    friendctl: str = ""
    client_names: dict[str, str] = field(default_factory=dict)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add a new AmneziaWG peer and write a .conf client profile."
    )
    parser.add_argument("server", help=f"Server name: {', '.join(sorted(SERVER_REGISTRY))}, or all with --stats")
    parser.add_argument("client", nargs="?", help="Client name, allowed: A-Z a-z 0-9 _ . -")
    parser.add_argument("--ssh-password")
    parser.add_argument("--ssh-user", help="Override SSH user from the registry")
    parser.add_argument("--ssh-timeout", type=int, default=60)
    parser.add_argument("--ssh-retries", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--address", help="Use an explicit client address, e.g. 10.8.1.12/32")
    parser.add_argument("--allowed-ips", default="0.0.0.0/0")
    parser.add_argument("--dns", help="Override client DNS line, e.g. '1.1.1.1, 8.8.8.8'")
    parser.add_argument("--mtu", type=int, help="Override client MTU")
    parser.add_argument("--endpoint", help="Override Cudy client endpoint, e.g. 195.170.35.108:51830")
    parser.add_argument("--persistent-keepalive", type=int, default=25)
    parser.add_argument("--stats", action="store_true", help="Print live peer statistics and exit")
    parser.add_argument("--list-servers", action="store_true", help="Print known server names and exit")
    parser.add_argument("--dry-run", action="store_true", help="Read server state, but do not modify it")
    parser.add_argument("--force", action="store_true", help="Allow overwriting an existing local output file")
    return parser.parse_args()


def known_server_names() -> list[str]:
    return sorted(SERVER_REGISTRY)


def load_server(name: str, ssh_user_override: str | None) -> Server:
    if name not in SERVER_REGISTRY:
        known = ", ".join(known_server_names())
        raise SystemExit(f"Unknown server: {name}. Known servers: {known}")
    cfg = dict(SERVER_REGISTRY[name])
    if ssh_user_override:
        cfg["ssh_user"] = ssh_user_override
    return Server(
        name=name,
        kind=cfg.get("kind", "docker-awg"),
        ssh_host=cfg["ssh_host"],
        ssh_user=cfg["ssh_user"],
        endpoint_host=cfg["endpoint_host"],
        endpoint_port=int(cfg["endpoint_port"]),
        client_network=ipaddress.ip_network(cfg["client_network"]),
        dns=cfg["dns"],
        mtu=int(cfg["mtu"]),
        docker_container=cfg.get("docker_container", ""),
        awg_interface=cfg.get("awg_interface", ""),
        friendctl=cfg.get("friendctl", ""),
        client_names=dict(cfg.get("client_names", {})),
    )


def server_password(server_name: str, explicit_password: str | None) -> str | None:
    if explicit_password:
        return explicit_password
    env_name = f"AWG_SSH_PASSWORD_{server_name.upper().replace('-', '_')}"
    password = os.environ.get(env_name) or os.environ.get("AWG_SSH_PASSWORD")
    if password:
        return password
    if server_name == "cudy-home" and DEFAULT_CUDY_PASSWORD_FILE.exists():
        password = DEFAULT_CUDY_PASSWORD_FILE.read_text(encoding="utf-8-sig").strip()
        if password:
            return password
    return None


class Remote:
    def __init__(self, server: Server, password: str, *, timeout: int, retries: int):
        self.server = server
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                self.client.connect(
                    server.ssh_host,
                    username=server.ssh_user,
                    password=password,
                    timeout=timeout,
                    banner_timeout=timeout,
                    auth_timeout=timeout,
                    look_for_keys=False,
                    allow_agent=False,
                )
                return
            except Exception as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(min(10, attempt * 3))
        raise RuntimeError(
            f"SSH connection failed after {retries} attempts to "
            f"{server.ssh_user}@{server.ssh_host}: {last_error}"
        ) from last_error

    def close(self) -> None:
        self.client.close()

    def run(self, command: str, *, stdin_data: str | None = None, timeout: int = 120) -> str:
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        if stdin_data is not None:
            stdin.write(stdin_data)
            stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        if rc:
            raise RuntimeError(f"Remote command failed rc={rc}: {command}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
        return out


def docker_exec(server: Server, command: str) -> str:
    return f"docker exec {shlex.quote(server.docker_container)} sh -lc {shlex.quote(command)}"


def docker_exec_raw(server: Server, command: str) -> str:
    return f"docker exec {shlex.quote(server.docker_container)} {command}"


def docker_exec_stdin(server: Server, command: str) -> str:
    return f"docker exec -i {shlex.quote(server.docker_container)} sh -lc {shlex.quote(command)}"


def parse_awg_conf(conf: str) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    interface: dict[str, str] = {}
    peers: list[tuple[str, str, str]] = []
    section = None
    public_key = ""
    preshared_key = ""
    allowed_ips = ""

    for raw in conf.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[Interface]":
            section = "interface"
            continue
        if line == "[Peer]":
            if public_key or allowed_ips:
                peers.append((public_key, preshared_key, allowed_ips))
            section = "peer"
            public_key = ""
            preshared_key = ""
            allowed_ips = ""
            continue
        if " = " not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        if section == "interface":
            interface[key] = value
        elif section == "peer":
            if key == "PublicKey":
                public_key = value
            elif key == "PresharedKey":
                preshared_key = value
            elif key == "AllowedIPs":
                allowed_ips = value

    if public_key or allowed_ips:
        peers.append((public_key, preshared_key, allowed_ips))
    return interface, peers


def used_addresses(peers: Iterable[tuple[str, str, str]]) -> set[ipaddress.IPv4Address]:
    used: set[ipaddress.IPv4Address] = set()
    for _, _, allowed in peers:
        for item in allowed.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                net = ipaddress.ip_network(item, strict=False)
            except ValueError:
                continue
            if isinstance(net, ipaddress.IPv4Network) and net.prefixlen == 32:
                used.add(net.network_address)
    return used


def next_client_address(network: ipaddress.IPv4Network, peers: Iterable[tuple[str, str, str]]) -> str:
    used = used_addresses(peers)
    for address in network.hosts():
        if address == network.network_address + 0:
            continue
        if address not in used:
            return f"{address}/32"
    raise RuntimeError(f"No free address in {network}")


def validate_name(name: str) -> None:
    if not SAFE_NAME_RE.fullmatch(name):
        raise SystemExit("Client name may contain only A-Z a-z 0-9 _ . -")


def output_path_for(server: Server, output_dir: Path, client_name: str | None) -> Path | None:
    if not client_name:
        return None
    if server.kind == "cudy-friends":
        filename = f"{client_name}.conf" if client_name.endswith("-awg") else f"{client_name}-awg.conf"
    else:
        filename = f"{client_name}.conf"
    return output_dir / server.name / filename


def format_bytes(value: str) -> str:
    try:
        size = int(value)
    except ValueError:
        return "-"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    amount = float(size)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{int(amount)} {unit}"
    return f"{amount:.2f} {unit}"


def format_handshake(value: str) -> str:
    try:
        ts = int(value)
    except ValueError:
        return "-"
    if ts <= 0:
        return "never"
    delta = max(0, int(datetime.now().timestamp()) - ts)
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h ago"
    if hours:
        return f"{hours}h {minutes}m ago"
    if minutes:
        return f"{minutes}m {seconds}s ago"
    return f"{seconds}s ago"


def parse_awg_dump(dump: str) -> dict[str, dict[str, str]]:
    stats: dict[str, dict[str, str]] = {}
    for index, line in enumerate(dump.splitlines()):
        if index == 0:
            continue
        cols = line.split("\t")
        if len(cols) < 7:
            continue
        stats[cols[0]] = {
            "endpoint": cols[2] if len(cols) > 2 and cols[2] != "(none)" else "-",
            "allowed_ips": cols[3] if len(cols) > 3 else "-",
            "latest_handshake": format_handshake(cols[4] if len(cols) > 4 else "0"),
            "rx": format_bytes(cols[5] if len(cols) > 5 else "0"),
            "tx": format_bytes(cols[6] if len(cols) > 6 else "0"),
            "keepalive": cols[7] if len(cols) > 7 and cols[7] != "off" else "-",
        }
    return stats


def local_config_client_names(server: Server) -> dict[str, str]:
    names: dict[str, str] = {}
    config_dir = DEFAULT_OUTPUT_DIR / server.name
    if not config_dir.exists():
        return names
    for path in config_dir.glob("*.conf"):
        try:
            text = path.read_text(encoding="ascii")
        except Exception:
            continue
        match = re.search(r"^Address\s*=\s*([^\s,]+)", text, re.MULTILINE)
        if not match:
            continue
        client_name = path.stem
        if server.kind == "cudy-friends" and client_name.endswith("-awg"):
            client_name = client_name[:-4]
        names.setdefault(match.group(1), client_name)
    return names


def print_peer_stats(remote: Remote, server: Server) -> None:
    if server.kind == "cudy-friends":
        print_cudy_peer_stats(remote, server)
        return
    if server.kind != "docker-awg":
        raise RuntimeError(f"Unsupported server kind for stats: {server.kind}")

    rows = read_clients_table(remote, server)
    names: dict[str, dict[str, str]] = {}
    for row in rows:
        public_key = str(row.get("clientId", ""))
        user_data = row.get("userData", {}) if isinstance(row.get("userData", {}), dict) else {}
        names[public_key] = {
            "name": str(user_data.get("clientName", "-")),
            "table_allowed_ips": str(user_data.get("allowedIps", "-")),
        }

    dump = remote.run(docker_exec_raw(server, f"awg show {shlex.quote(server.awg_interface)} dump"))
    live = parse_awg_dump(dump)
    address_names = dict(server.client_names)
    address_names.update(local_config_client_names(server))
    keys = sorted(
        set(names) | set(live),
        key=lambda key: (
            ipaddress.ip_network(live.get(key, {}).get("allowed_ips", names.get(key, {}).get("table_allowed_ips", "255.255.255.255/32")).split(",")[0], strict=False).network_address
            if re.match(r"^\d+\.\d+\.\d+\.\d+/\d+", live.get(key, {}).get("allowed_ips", names.get(key, {}).get("table_allowed_ips", "")))
            else ipaddress.ip_address("255.255.255.255")
        ),
    )

    header = f"{'name':22} {'allowed_ips':18} {'endpoint':24} {'handshake':14} {'rx':12} {'tx':12} {'keepalive':9}"
    print(f"\n== {server.name} ({server.endpoint_host}:{server.endpoint_port}) ==")
    print(header)
    print("-" * len(header))
    for key in keys:
        name = names.get(key, {}).get("name", "-")
        live_row = live.get(key, {})
        allowed = live_row.get("allowed_ips") or names.get(key, {}).get("table_allowed_ips", "-")
        first_allowed = allowed.split(",", 1)[0].strip()
        if not name or name == "-":
            name = address_names.get(first_allowed, "-")
        print(
            f"{name[:22]:22} "
            f"{allowed[:18]:18} "
            f"{live_row.get('endpoint', '-')[:24]:24} "
            f"{live_row.get('latest_handshake', '-')[:14]:14} "
            f"{live_row.get('rx', '-')[:12]:12} "
            f"{live_row.get('tx', '-')[:12]:12} "
            f"{live_row.get('keepalive', '-')[:9]:9}"
        )


def print_cudy_peer_stats(remote: Remote, server: Server) -> None:
    output = remote.run(f"{shlex.quote(server.friendctl)} list").rstrip()
    print(f"\n== {server.name} ({server.endpoint_host}:{server.endpoint_port}, ssh={server.ssh_host}) ==")
    print(output)


def print_all_peer_stats(args: argparse.Namespace) -> int:
    failures: list[str] = []
    for server_name in known_server_names():
        server = load_server(server_name, args.ssh_user)
        password = server_password(server.name, args.ssh_password)
        if not password:
            failures.append(
                f"{server.name}: missing password "
                f"(set AWG_SSH_PASSWORD or AWG_SSH_PASSWORD_{server.name.upper().replace('-', '_')})"
            )
            continue
        remote: Remote | None = None
        try:
            remote = Remote(server, password, timeout=args.ssh_timeout, retries=args.ssh_retries)
            print_peer_stats(remote, server)
        except Exception as exc:
            failures.append(f"{server.name}: {exc}")
        finally:
            if remote:
                remote.close()
    if failures:
        print("\n== errors ==")
        for failure in failures:
            print(f"- {failure}")
        return 2
    return 0


def validate_key(name: str, key: str) -> None:
    if not KEY_RE.fullmatch(key):
        raise RuntimeError(f"Unexpected {name} format: {key!r}")


def build_client_conf(
    *,
    server: Server,
    interface: dict[str, str],
    client_private_key: str,
    client_address: str,
    server_public_key: str,
    psk: str,
    allowed_ips: str,
    keepalive: int,
    dns: str | None = None,
    mtu: int | None = None,
) -> str:
    awg_lines = []
    for key in ["Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5"]:
        if key in interface and interface[key] != "":
            awg_lines.append(f"{key} = {interface[key]}")
    awg_block = "\n".join(awg_lines)
    return f"""[Interface]
PrivateKey = {client_private_key}
Address = {client_address}
DNS = {dns or server.dns}
MTU = {mtu or server.mtu}
{awg_block}

[Peer]
PublicKey = {server_public_key}
PresharedKey = {psk}
Endpoint = {server.endpoint_host}:{server.endpoint_port}
AllowedIPs = {allowed_ips}
PersistentKeepalive = {keepalive}
"""


def tune_client_conf(conf: str, *, dns: str | None = None, mtu: int | None = None) -> str:
    if dns:
        conf = re.sub(r"^DNS\s*=.*$", f"DNS = {dns}", conf, count=1, flags=re.MULTILINE)
    if mtu:
        conf = re.sub(r"^MTU\s*=.*$", f"MTU = {mtu}", conf, count=1, flags=re.MULTILINE)
    return conf


def read_clients_table(remote: Remote, server: Server) -> list[dict]:
    try:
        raw = remote.run(docker_exec(server, "cat /opt/amnezia/awg/clientsTable 2>/dev/null || printf '[]'"))
        data = json.loads(raw or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def write_clients_table(remote: Remote, server: Server, rows: list[dict]) -> None:
    text = json.dumps(rows, ensure_ascii=False, indent=4)
    remote.run(
        docker_exec_stdin(server, "cat > /opt/amnezia/awg/clientsTable"),
        stdin_data=text + "\n",
    )


def add_client_table_row(rows: list[dict], client_public_key: str, client_name: str, client_address: str) -> list[dict]:
    rows = [row for row in rows if row.get("clientId") != client_public_key]
    rows.append(
        {
            "clientId": client_public_key,
            "userData": {
                "allowedIps": client_address,
                "clientName": client_name,
                "creationDate": datetime.now().strftime("%a %b %d %H:%M:%S %Y"),
            },
        }
    )
    return rows


def cudy_endpoint(server: Server, endpoint_override: str | None) -> str:
    endpoint = endpoint_override or f"{server.endpoint_host}:{server.endpoint_port}"
    if not re.fullmatch(r"[^:\s]+:\d{1,5}", endpoint):
        raise SystemExit("--endpoint must look like host:port")
    port = int(endpoint.rsplit(":", 1)[1])
    if port < 1 or port > 65535:
        raise SystemExit("--endpoint port must be between 1 and 65535")
    return endpoint


def read_cudy_friend_rows(remote: Remote, server: Server) -> list[dict[str, str]]:
    output = remote.run(f"{shlex.quote(server.friendctl)} list")
    rows: list[dict[str, str]] = []
    for index, line in enumerate(output.splitlines()):
        if index == 0 or not line.strip():
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        rows.append(
            {
                "name": parts[0],
                "ip": parts[1],
                "enabled": parts[2],
                "endpoint": parts[3],
                "handshake": parts[4],
                "from_peer_bytes": parts[5],
                "to_peer_bytes": parts[6],
            }
        )
    return rows


def next_cudy_address(server: Server, rows: list[dict[str, str]]) -> str:
    used: set[ipaddress.IPv4Address] = set()
    for row in rows:
        try:
            used.add(ipaddress.ip_address(row["ip"]))
        except ValueError:
            continue
    for address in server.client_network.hosts():
        if int(address) == int(server.client_network.network_address) + 1:
            continue
        if address not in used:
            return f"{address}/32"
    raise RuntimeError(f"No free address in {server.client_network}")


def add_cudy_client(remote: Remote, server: Server, args: argparse.Namespace, output_path: Path) -> int:
    if args.address:
        raise SystemExit("--address is not supported for cudy-home; friendctl chooses the next free 10.77.0.x address")
    endpoint = cudy_endpoint(server, args.endpoint)
    rows = read_cudy_friend_rows(remote, server)
    peer_exists = any(row["name"] == args.client for row in rows)
    next_address = next_cudy_address(server, rows)

    if args.dry_run:
        print(f"server={server.name}")
        print(f"endpoint={endpoint}")
        print(f"peer_exists={str(peer_exists).lower()}")
        if not peer_exists:
            print(f"next_address={next_address}")
        print(f"output={output_path}")
        return 0

    if not peer_exists:
        remote.run(
            f"{shlex.quote(server.friendctl)} add {shlex.quote(args.client)} {shlex.quote(endpoint)}",
            timeout=180,
        )
    conf = remote.run(f"{shlex.quote(server.friendctl)} conf {shlex.quote(args.client)}")
    conf = tune_client_conf(conf, dns=args.dns, mtu=args.mtu)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(conf, encoding="ascii")
    print(f"{'saved_existing' if peer_exists else 'created'}={output_path}")
    print(f"server={server.name}")
    print(f"endpoint={endpoint}")
    if peer_exists:
        for row in rows:
            if row["name"] == args.client:
                print(f"address={row['ip']}/32")
                break
    else:
        print(f"address={next_address}")
    return 0


def main() -> int:
    args = parse_args()
    if args.list_servers:
        for name in known_server_names():
            cfg = SERVER_REGISTRY[name]
            kind = cfg.get("kind", "docker-awg")
            print(f"{name}\t{kind}\t{cfg['endpoint_host']}:{cfg['endpoint_port']}\tssh={cfg['ssh_user']}@{cfg['ssh_host']}")
        return 0

    if args.stats:
        if args.client:
            raise SystemExit("--stats does not take a client name")
    else:
        if not args.client:
            raise SystemExit("Client name is required unless --stats is used")
        validate_name(args.client)

    if args.server == "all":
        if not args.stats:
            raise SystemExit("server=all is supported only with --stats")
        return print_all_peer_stats(args)

    server = load_server(args.server, args.ssh_user)
    password = server_password(server.name, args.ssh_password)
    if not password:
        raise SystemExit(
            "Set AWG_SSH_PASSWORD, pass --ssh-password, or set "
            f"AWG_SSH_PASSWORD_{server.name.upper().replace('-', '_')}"
        )

    output_path = output_path_for(server, args.output_dir, args.client)
    if output_path and output_path.exists() and not args.force:
        raise SystemExit(f"Output already exists: {output_path}. Use --force to overwrite.")

    remote = Remote(server, password, timeout=args.ssh_timeout, retries=args.ssh_retries)
    try:
        if args.stats:
            print_peer_stats(remote, server)
            return 0
        if server.kind == "cudy-friends":
            assert output_path is not None
            return add_cudy_client(remote, server, args, output_path)
        if server.kind != "docker-awg":
            raise RuntimeError(f"Unsupported server kind: {server.kind}")

        remote.run(docker_exec(server, "test -f /opt/amnezia/awg/awg0.conf"))
        awg_conf = remote.run(docker_exec(server, "cat /opt/amnezia/awg/awg0.conf"))
        interface, peers = parse_awg_conf(awg_conf)
        if not interface:
            raise RuntimeError("Could not parse [Interface] from awg0.conf")

        client_address = args.address or next_client_address(server.client_network, peers)
        if ipaddress.ip_interface(client_address).network.prefixlen != 32:
            raise SystemExit("--address must be a /32 client address")
        if ipaddress.ip_interface(client_address).ip not in server.client_network:
            raise SystemExit(f"{client_address} is outside {server.client_network}")
        if ipaddress.ip_interface(client_address).ip in used_addresses(peers):
            raise SystemExit(f"Address is already used on server: {client_address}")

        if args.dry_run:
            print(f"server={server.name}")
            print(f"endpoint={server.endpoint_host}:{server.endpoint_port}")
            print(f"next_address={client_address}")
            print(f"output={output_path}")
            return 0

        key_output = remote.run(
            docker_exec(server, 'k=$(awg genkey); p=$(printf "%s\\n" "$k" | awg pubkey); s=$(awg genpsk); printf "%s\\n%s\\n%s\\n" "$k" "$p" "$s"')
        ).strip().splitlines()
        if len(key_output) != 3:
            raise RuntimeError(f"Unexpected key generation output: {key_output!r}")
        client_private_key, client_public_key, psk = key_output
        validate_key("client private key", client_private_key)
        validate_key("client public key", client_public_key)
        validate_key("preshared key", psk)

        server_public_key = remote.run(docker_exec(server, "cat /opt/amnezia/awg/wireguard_server_public_key.key")).strip()
        validate_key("server public key", server_public_key)

        peer_block = f"\n[Peer]\nPublicKey = {client_public_key}\nPresharedKey = {psk}\nAllowedIPs = {client_address}\n"
        remote.run(docker_exec_stdin(server, "cat >> /opt/amnezia/awg/awg0.conf"), stdin_data=peer_block)
        remote.run(docker_exec_stdin(server, "umask 077; cat > /tmp/awg-client-add.psk"), stdin_data=psk + "\n")
        remote.run(
            docker_exec_raw(
                server,
                f"awg set {shlex.quote(server.awg_interface)} peer {shlex.quote(client_public_key)} "
                f"preshared-key /tmp/awg-client-add.psk allowed-ips {shlex.quote(client_address)} "
                f"persistent-keepalive {int(args.persistent_keepalive)}",
            )
        )
        remote.run(docker_exec(server, "rm -f /tmp/awg-client-add.psk"))

        rows = read_clients_table(remote, server)
        rows = add_client_table_row(rows, client_public_key, args.client, client_address)
        write_clients_table(remote, server, rows)

        image = remote.run(
            f"docker inspect {shlex.quote(server.docker_container)} --format '{{{{.Config.Image}}}}'"
        ).strip()
        if image:
            remote.run(f"docker commit {shlex.quote(server.docker_container)} {shlex.quote(image)} >/dev/null", timeout=180)

        conf = build_client_conf(
            server=server,
            interface=interface,
            client_private_key=client_private_key,
            client_address=client_address,
            server_public_key=server_public_key,
            psk=psk,
            allowed_ips=args.allowed_ips,
            keepalive=args.persistent_keepalive,
            dns=args.dns,
            mtu=args.mtu,
        )
        assert output_path is not None
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(conf, encoding="ascii")
        print(f"created={output_path}")
        print(f"server={server.name}")
        print(f"endpoint={server.endpoint_host}:{server.endpoint_port}")
        print(f"address={client_address}")
        return 0
    finally:
        remote.close()


if __name__ == "__main__":
    sys.exit(main())
