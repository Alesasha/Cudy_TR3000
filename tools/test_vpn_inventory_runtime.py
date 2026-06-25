#!/usr/bin/env python3
"""Regression checks for Cudy runtime inventory parsing."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import vpn_inventory  # noqa: E402


def test_provider_refresh_schedule_status() -> None:
    status = vpn_inventory.provider_refresh_schedule_status(
        """
# comment
7 5 * * * /usr/bin/vpntype-proxy-refresh-all
29 5 * * * /usr/bin/lokvpn-refresh-current

"""
    )
    assert status["ok"] is True
    assert status["vpntype"]["present"] is True
    assert status["lokvpn"]["present"] is True
    assert len(status["active_entries"]) == 2


def test_provider_refresh_schedule_missing_lokvpn_warns() -> None:
    status = vpn_inventory.provider_refresh_schedule_status("7 5 * * * /usr/bin/vpntype-proxy-refresh-all\n")
    assert status["ok"] is False
    assert status["vpntype"]["present"] is True
    assert status["lokvpn"]["present"] is False


if __name__ == "__main__":
    test_provider_refresh_schedule_status()
    test_provider_refresh_schedule_missing_lokvpn_warns()
    print("VPN inventory runtime regression passed.")
