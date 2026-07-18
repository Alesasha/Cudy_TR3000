#!/usr/bin/env python3
"""Capture a redacted, read-only Cudy/OpenWrt preflight snapshot."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from deploy_cudy_go_fallback import ROOT, connect, load_password, ssh_exec


DEFAULT_OUTPUT_ROOT = ROOT / "backups" / "cudy" / "snapshots"
SENSITIVE_UCI_OPTION = re.compile(
    r"(?i)(?:^|\.)(?:key|password|passwd|private_key|preshared_key|secret|token)="
)

COMMANDS: tuple[tuple[str, str], ...] = (
    ("system_board", "ubus call system board 2>/dev/null || true"),
    ("ip_addr", "ip -4 addr 2>/dev/null || true"),
    ("ip_route", "ip -4 route show table all 2>/dev/null || ip -4 route 2>/dev/null || true"),
    ("ip_link", "ip -d link show eth0 2>/dev/null || ip -d link 2>/dev/null || true"),
    ("wan_device", "ubus call network.device status '{\"name\":\"eth0\"}' 2>/dev/null || true"),
    ("uci_network", "uci show network 2>/dev/null || true"),
    ("uci_dhcp", "uci show dhcp 2>/dev/null || true"),
    ("uci_firewall", "uci show firewall 2>/dev/null || true"),
    ("uci_wireless", "uci show wireless 2>/dev/null || true"),
    ("uci_system", "uci show system 2>/dev/null || true"),
    ("uci_dropbear", "uci show dropbear 2>/dev/null || true"),
    ("uci_uhttpd", "uci show uhttpd 2>/dev/null || true"),
    (
        "router_agent",
        "printf 'service='; /etc/init.d/cudy-router-agent status 2>/dev/null || true; "
        "printf '\\nconfig='; uci show cudy-router-agent 2>/dev/null || true; "
        "printf '\\nstatus='; cat /var/lib/cudy-router-agent/status.json 2>/dev/null || true; printf '\\n'",
    ),
)


def redact_output(text: str) -> str:
    """Redact UCI assignments that may contain credentials or private keys."""
    redacted: list[str] = []
    for line in text.splitlines():
        if "=" in line and SENSITIVE_UCI_OPTION.search(line):
            key, _value = line.split("=", 1)
            line = f"{key}='<redacted>'"
        redacted.append(line)
    return "\n".join(redacted) + ("\n" if text.endswith("\n") else "")


def capture_snapshot(
    *,
    host: str,
    user: str,
    password: str,
    timeout: int,
    output_root: Path,
) -> Path:
    generated_at = dt.datetime.now(dt.timezone.utc)
    snapshot = output_root / generated_at.astimezone().strftime("%Y%m%d-%H%M%S")
    snapshot.mkdir(parents=True, exist_ok=False)
    entries: list[dict[str, Any]] = []

    client = connect(host, user, password, timeout)
    try:
        for name, command in COMMANDS:
            rc, output = ssh_exec(client, command, timeout)
            safe_output = redact_output(output)
            path = snapshot / f"{name}.txt"
            path.write_text(safe_output, encoding="utf-8")
            entries.append(
                {
                    "name": name,
                    "file": path.name,
                    "command": command,
                    "exit_code": rc,
                    "bytes": len(safe_output.encode("utf-8")),
                }
            )
    except Exception:
        # Preserve partial evidence and mark it clearly before propagating failure.
        (snapshot / "capture-error.txt").write_text(
            "Snapshot capture failed before all read-only commands completed.\n",
            encoding="utf-8",
        )
        raise
    finally:
        client.close()

    index = {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "host": host,
        "read_only": True,
        "redacted": True,
        "commands": entries,
    }
    (snapshot / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.8.1")
    parser.add_argument("--user", default="root")
    parser.add_argument("--ssh-password")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    snapshot = capture_snapshot(
        host=args.host,
        user=args.user,
        password=load_password(args.ssh_password),
        timeout=args.timeout,
        output_root=args.output_root,
    )
    print(f"Captured read-only redacted Cudy snapshot: {snapshot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
