#!/usr/bin/env python3
"""Regression checks for the guarded Cudy transport bootstrap."""

from __future__ import annotations

from pathlib import Path

from trial_cudy_transport_bootstrap import (
    build_parser,
    rollback_script,
    transport_paths,
    validate_preflight,
)


ROOT = Path(__file__).resolve().parents[1]


def healthy_state() -> dict:
    return {
        "service": "running",
        "configured_mode": "observe",
        "allow_apply": "0",
        "status": {"mode": "observe", "ok": True},
        "desired": {
            "blockers": [],
            "transport_actions": [
                {
                    "server_id": "proxykz",
                    "interface": "proxykz",
                    "transport_type": "http-proxy-tun",
                    "action": "prepare-and-start",
                    "config_path": "/etc/sing-box/proxykz.json",
                    "service": "sing-box-proxykz",
                }
            ],
        },
        "diff": [],
    }


def main() -> int:
    defaults = build_parser().parse_args([])
    assert not defaults.apply
    assert not defaults.yes
    assert not defaults.commit
    assert defaults.trial_seconds >= 120

    state = healthy_state()
    assert validate_preflight(state) == []
    assert transport_paths(state["desired"]["transport_actions"]) == [
        "/etc/config/pbr",
        "/etc/init.d/sing-box-proxykz",
        "/etc/sing-box/proxykz.json",
    ]

    unsafe = healthy_state()
    unsafe["desired"]["transport_actions"][0]["service"] = "dropbear"
    assert any("unexpected transport service" in item for item in validate_preflight(unsafe))

    unsafe = healthy_state()
    unsafe["desired"]["transport_actions"][0]["config_path"] = "/etc/config/network"
    assert any("unsafe transport config path" in item for item in validate_preflight(unsafe))

    guard = rollback_script(
        "/root/cudy-transport-trials/test",
        state["desired"]["transport_actions"],
        300,
    )
    assert "sleep \"$delay\"" in guard
    assert "[ -f \"$trial/commit\" ] && exit 0" in guard
    assert "[ -f \"$trial/rolled-back\" ] && exit 0" in guard
    assert "/etc/init.d/cudy-router-agent stop" in guard
    assert "/usr/bin/cudy-pbr-safe-restart" in guard
    assert "service-proxykz.running" not in guard
    assert "service-$service.running" in guard
    source = Path(__file__).with_name("trial_cudy_transport_bootstrap.py").read_text(encoding="utf-8")
    assert "/sbin/start-stop-daemon -S -b -m" in source
    assert "test -f \"$trial/armed\"" in source
    assert "touch \"$trial/armed\"" in guard
    source = (ROOT / "tools" / "trial_cudy_transport_bootstrap.py").read_text(encoding="utf-8")
    assert "/etc/init.d/pbr status" not in source
    assert "nohup" not in source
    print("Guarded Cudy transport bootstrap regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
