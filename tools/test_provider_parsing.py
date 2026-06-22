#!/usr/bin/env python3
"""Local regression checks for provider transport parsing."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import vpn_control_app as app  # noqa: E402


def lokvpn_sample() -> list[dict[str, Any]]:
    def outbound(tag: str, port: int) -> dict[str, Any]:
        return {
            "tag": tag,
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": "203.0.113.10",
                        "port": port,
                        "users": [{"id": "00000000-0000-0000-0000-000000000001", "flow": "xtls-rprx-vision"}],
                    }
                ]
            },
            "streamSettings": {
                "realitySettings": {
                    "serverName": "vk.ru",
                    "publicKey": "public-key",
                    "shortId": "abcd",
                }
            },
        }

    return [{"outbounds": [outbound(" RU ", 8080), outbound("DE", 8282), {"tag": "direct", "protocol": "freedom"}]}]


def assert_equal(actual: Any, expected: Any, context: str) -> None:
    if actual != expected:
        raise AssertionError(f"{context}: expected {expected!r}, got {actual!r}")


def main() -> int:
    sample = lokvpn_sample()
    de = app.parse_lokvpn_outbound("de1", app.find_lokvpn_outbound(sample, "de1"))
    ru = app.parse_lokvpn_outbound("ru1", app.find_lokvpn_outbound(sample, "ru1"))
    smart = app.parse_lokvpn_outbound("smart2", app.find_lokvpn_outbound(sample, "smart2"))
    assert_equal(de["server_port"], 8282, "DE tag port")
    assert_equal(ru["server_port"], 8080, "RU tag port")
    assert_equal(smart["server_port"], 8080, "smart2 fallback order")
    try:
        app.find_lokvpn_outbound(sample, "nl1")
    except ValueError as exc:
        assert "available tags: DE, RU" in str(exc), str(exc)
    else:
        raise AssertionError("missing LokVPN profile should raise")
    print("Provider parsing regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
