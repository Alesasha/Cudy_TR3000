#!/usr/bin/env python3
"""Static smoke checks for the Android agent production UI surface."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAYOUT = ROOT / "apps" / "CudyAndroidAgent" / "Resources" / "layout" / "activity_main.xml"
MAIN_ACTIVITY = ROOT / "apps" / "CudyAndroidAgent" / "MainActivity.cs"
BOOT_RECEIVER = ROOT / "apps" / "CudyAndroidAgent" / "BootReceiver.cs"
VPN_SERVICE = ROOT / "apps" / "CudyAndroidAgent" / "CudyVpnService.cs"
CRITICAL_MONITOR = ROOT / "apps" / "CudyAndroidAgent" / "CudyCriticalServiceMonitor.cs"


ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


def layout_ids() -> set[str]:
    tree = ET.parse(LAYOUT)
    found: set[str] = set()
    for element in tree.iter():
        value = element.attrib.get(f"{ANDROID_NS}id")
        if value and value.startswith("@+id/"):
            found.add(value.removeprefix("@+id/"))
    return found


def assert_contains(path: Path, needles: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    for needle in needles:
        if needle not in text:
            raise AssertionError(f"{path.relative_to(ROOT)} is missing {needle!r}")


def main() -> int:
    ids = layout_ids()
    required_ids = {
        "startButton",
        "stopButton",
        "statusButton",
        "updateButton",
        "autostartCheckBox",
        "enrollmentCodeInput",
        "enrollButton",
        "loadUiButton",
        "defaultServerInput",
        "saveDefaultButton",
        "domainInput",
        "domainServerInput",
        "saveDomainButton",
        "lookupInput",
        "lookupButton",
    }
    missing = sorted(required_ids - ids)
    if missing:
        raise AssertionError(f"activity_main.xml is missing ids: {', '.join(missing)}")

    assert_contains(
        MAIN_ACTIVITY,
        [
            '"/api/agent/bootstrap"',
            '"/api/agent/user-default-server"',
            '"/api/agent/domain-routes"',
            '"/api/agent/route-lookup?target=',
            '"/api/agent/app-version?platform=android"',
            '"autostart_enabled"',
            "EnrollDeviceAsync",
            "LoadUserUiAsync",
            "CheckUpdateAsync",
        ],
    )
    assert_contains(
        BOOT_RECEIVER,
        [
            '"autostart_enabled"',
            "skipped-autostart-disabled",
        ],
    )
    assert_contains(
        CRITICAL_MONITOR,
        [
            'TryGetProperty("critical_services"',
            'GetString(item, "success_pattern")',
            'GetString(item, "failure_pattern")',
            "RegexTimeout",
            "CheckAsync",
        ],
    )
    assert_contains(
        VPN_SERVICE,
        [
            "consecutiveCriticalFailures",
            "CudyCriticalServiceMonitor.CheckAsync",
            "stop_vpn_restore_direct",
            '"/api/agent/diagnostics"',
        ],
    )
    print("Android agent UI static smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
