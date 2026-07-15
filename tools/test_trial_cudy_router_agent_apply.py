#!/usr/bin/env python3
"""Regression checks for the guarded Cudy apply trial."""

from __future__ import annotations

from datetime import datetime, timezone

from trial_cudy_router_agent_apply import build_parser, rollback_script, validate_preflight


def healthy_state() -> dict:
    return {
        "service": "running",
        "configured_mode": "observe",
        "allow_apply": "0",
        "status": {
            "mode": "observe",
            "ok": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "route_count": 22,
            "critical_service_count": 5,
            "critical_services_ok": True,
        },
        "desired": {"blockers": [], "transport_actions": []},
        "diff": [{"path": "/etc/pbr-overrides/force-proxynl.ips", "added": ["1.2.3.4/32"]}],
    }


def main() -> int:
    defaults = build_parser().parse_args([])
    assert not defaults.apply
    assert not defaults.yes
    assert not defaults.commit
    assert defaults.trial_seconds >= 120
    assert validate_preflight(healthy_state(), max_age_seconds=300) == []

    unsafe = healthy_state()
    unsafe["diff"] = [{"path": "/etc/config/network"}]
    assert any("unsafe trial path" in item for item in validate_preflight(unsafe, max_age_seconds=300))

    transport = healthy_state()
    transport["desired"]["transport_actions"] = [{"server_id": "new-exit"}]
    assert any("refuses plans" in item for item in validate_preflight(transport, max_age_seconds=300))

    guard = rollback_script("/root/cudy-router-trials/test", 300)
    assert "sleep 300" in guard
    assert "[ -f \"$trial/commit\" ] && exit 0" in guard
    assert "cudy-router-agent.main.mode='observe'" in guard
    assert "/usr/bin/cudy-pbr-safe-restart" in guard
    assert "/etc/init.d/pbr stop" in guard
    assert "managed-paths.next.json" in guard
    assert "pbr.was-running" in guard
    print("Guarded Cudy apply trial regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
