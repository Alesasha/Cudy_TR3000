#!/usr/bin/env python3
"""Static smoke checks for the Android agent production UI surface."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAYOUT = ROOT / "apps" / "CudyAndroidAgent" / "Resources" / "layout" / "activity_main.xml"
ADMIN_LAYOUT = ROOT / "apps" / "CudyAndroidAgent" / "Resources" / "layout" / "activity_admin.xml"
MAIN_ACTIVITY = ROOT / "apps" / "CudyAndroidAgent" / "MainActivity.cs"
ADMIN_ACTIVITY = ROOT / "apps" / "CudyAndroidAgent" / "AdminActivity.cs"
ADMIN_SESSION = ROOT / "apps" / "CudyAndroidAgent" / "CudyAdminSession.cs"
BOOT_RECEIVER = ROOT / "apps" / "CudyAndroidAgent" / "BootReceiver.cs"
VPN_SERVICE = ROOT / "apps" / "CudyAndroidAgent" / "CudyVpnService.cs"
CRITICAL_MONITOR = ROOT / "apps" / "CudyAndroidAgent" / "CudyCriticalServiceMonitor.cs"
SING_BOX_CONFIG = ROOT / "apps" / "CudyAndroidAgent" / "CudySingBoxConfig.cs"
ANDROID_PROBE = ROOT / "apps" / "CudyAndroidAgent" / "CudyAndroidProbe.cs"
SSH_CONTROL = ROOT / "apps" / "CudyAndroidAgent" / "CudySshControl.cs"
PROJECT = ROOT / "apps" / "CudyAndroidAgent" / "CudyAndroidAgent.csproj"


ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


def layout_ids(path: Path) -> set[str]:
    tree = ET.parse(path)
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
    ids = layout_ids(LAYOUT)
    required_ids = {
        "startButton",
        "stopButton",
        "statusButton",
        "updateButton",
        "adminButton",
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

    admin_ids = layout_ids(ADMIN_LAYOUT)
    required_admin_ids = {
        "adminLoginButton",
        "adminUsersList",
        "adminDevicesList",
        "adminSaveUserButton",
        "adminCreateCodeButton",
        "adminShareProvisioningButton",
    }
    missing_admin = sorted(required_admin_ids - admin_ids)
    if missing_admin:
        raise AssertionError(f"activity_admin.xml is missing ids: {', '.join(missing_admin)}")

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
            "typeof(AdminActivity)",
            'EnrollmentBootstrapUser = "cudy-enroll"',
            "EnrollmentBootstrapPort = 8766",
            "ReadEnrollmentBootstrapKey",
            'RequiredJsonString(provisioning, "ssh_private_key")',
            '"miui_autostart_confirmation_pending"',
            '"miui_autostart_confirmed"',
            "ConfirmMiuiAutostartIfPending",
            'dialog.SetPositiveButton("Enabled"',
        ],
    )
    assert_contains(
        ADMIN_ACTIVITY,
        [
            'Exported = false',
            '"/api/admin/users"',
            '"/api/admin/agent-devices"',
            '"/api/admin/enrollment-codes"',
            "EditDeviceAsync",
            "display_name =",
            'agent_only =',
            'activationCode = code',
            'Intent.ActionSend',
            'Share activation code',
        ],
    )
    assert_contains(SSH_CONTROL, ["uint remotePort = 8765", "remotePort)"])
    assert_contains(
        PROJECT,
        [
            "android_enrollment_bootstrap_ed25519",
            "EnsureEnrollmentBootstrapKey",
            "<ApplicationVersion>27</ApplicationVersion>",
        ],
    )
    main_text = MAIN_ACTIVITY.read_text(encoding="utf-8")
    for forbidden in (
        'GetString("device_id", "isasha_X7Pro_Cudy-android")',
        "importProvisioningButton",
        "ProvisioningFileRequest",
        "cudyagent://provision",
    ):
        if forbidden in main_text:
            raise AssertionError(f"MainActivity.cs still contains removed provisioning value: {forbidden}")
    assert_contains(
        ADMIN_SESSION,
        [
            'CudyVpnService.HasSharedControl',
            'RunSharedControlRequestAsync',
            '"/api/login"',
            '"/api/admin"',
            'This account does not have the admin role',
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
            "options.Inet4RouteRange",
            "AddTunDnsServers",
            "AddDisallowedApplication",
            "Added Android auto routes",
            "CudyAndroidProbeRunner.BuildLocalProbes(transportPlan)",
            "Duplicate start request ignored; control loop and TUN remain active",
            "StartFingerprint(",
        ],
    )
    assert_contains(
        ROOT / "apps" / "CudyAndroidAgent" / "CudyTransportStore.cs",
        [
            'Guid.NewGuid().ToString("N")',
            "File.Move(tempPath, path, overwrite: true)",
        ],
    )
    assert_contains(
        ANDROID_PROBE,
        [
            "BuildLocalProbes(CudyTransportPlan transportPlan)",
            "var probePorts = BuildLocalProbes(transportPlan)",
            '"local_mixed_proxy"',
        ],
    )
    probe_text = ANDROID_PROBE.read_text(encoding="utf-8")
    if ".StartOrReload(" in probe_text:
        raise AssertionError("Android probe runner must not reload the active VPN engine")
    assert_contains(
        ROOT / "apps" / "CudyAndroidAgent" / "CudyAndroidLibboxEngine.cs",
        [
            "MinimumReloadInterval",
            "libbox config reload deferred",
            "libbox pending config cancelled",
            "reload_deferred=",
        ],
    )
    assert_contains(
        BOOT_RECEIVER,
        [
            "CreateDeviceProtectedStorageContext",
            "userManager?.IsUserUnlocked",
            "waiting for user unlock before starting agent",
        ],
    )
    assert_contains(
        SING_BOX_CONFIG,
        [
            '["dns_mode"] = "hijack"',
            '["action"] = "sniff"',
            '["action"] = "hijack-dns"',
            '["auto_detect_interface"] = true',
            '["reverse_mapping"] = true',
            '["type"] = "fakeip"',
            '["inet4_range"] = "198.18.0.0/15"',
            'CollectTunneledDomainSuffixes(',
            '["exclude_package"] = new JsonArray { "com.nashvpn.cudyagent" }',
            "is unavailable on Android and will be blocked",
            'outboundTags[entry.ServerId] = "block"',
        ],
    )
    print("Android agent UI static smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
