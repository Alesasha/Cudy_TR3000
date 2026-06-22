#!/usr/bin/env python3
"""Local regression checks for route_agent plan behavior."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import route_agent  # noqa: E402


def main() -> int:
    action, command = route_agent.windows_route_command_for_ip(
        ip="203.0.113.1",
        server_id="auto",
        interface_map={},
        default_route={"interface_index": "14", "via": "192.168.8.1"},
    )
    if action != "direct" or "--interface-map" in command or "192.168.8.1" not in command:
        raise AssertionError(f"Windows unresolved auto should be direct, got {action=} {command=}")
    action, command = route_agent.route_command_for_ip(
        ip="203.0.113.1",
        server_id="auto",
        interface_map={},
        default_route={"dev": "eth0", "via": "192.168.1.1"},
    )
    if action != "direct" or "--interface-map" in command or "192.168.1.1" not in command:
        raise AssertionError(f"Linux unresolved auto should be direct, got {action=} {command=}")
    print("Route agent plan regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
