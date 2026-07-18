#!/usr/bin/env python3
"""Static safety regression for the disarmed main-router cutover guard."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    guard = (ROOT / "openwrt" / "cudy-main-router-guard").read_text(encoding="utf-8")
    init = (ROOT / "openwrt" / "cudy-main-router-guard.init").read_text(encoding="utf-8")
    deploy = (ROOT / "tools" / "deploy_cudy_main_router_guard.py").read_text(encoding="utf-8")

    required_guard = (
        '[ -e "$STATE_DIR/armed" ] || return 0',
        "make_backup",
        "verify_backup",
        "etc/config/network",
        "etc/config/dhcp",
        "etc/config/firewall",
        "etc/config/wireless",
        'now" -ge "$grace_until',
        "cutover was not committed before deadline",
        "expected LAN address",
        "IPv4 default route missing",
        "WAN gateway",
        'failures" -ge "$threshold',
        'tar -xzf "$BACKUP" -C /',
        'rm -f "$STATE_DIR/armed"',
        "/sbin/reboot",
    )
    for marker in required_guard:
        assert marker in guard, marker
    assert "curl" not in guard and "wget" not in guard, "external outage must not trigger config rollback"
    assert "USE_PROCD=1" in init
    assert "cudy-main-router-guard run" in init
    assert "--backup-now" in deploy
    assert "--check-only" in deploy
    assert 'fields.get("armed") == "no"' in deploy
    assert "--arm" not in deploy, "the installer must not arm a live rollback guard"
    print("Cudy main-router guard regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
