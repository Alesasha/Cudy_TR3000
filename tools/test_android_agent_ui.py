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
UPDATER = ROOT / "apps" / "CudyAndroidAgent" / "CudyAndroidUpdater.cs"
UPDATE_JOB = ROOT / "apps" / "CudyAndroidAgent" / "CudyUpdateJobService.cs"
UPDATE_RECEIVER = ROOT / "apps" / "CudyAndroidAgent" / "CudyUpdateInstallReceiver.cs"
MANIFEST = ROOT / "apps" / "CudyAndroidAgent" / "AndroidManifest.xml"
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
        "statusDetailText",
        "statusButton",
        "toggleRoutingButton",
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
        "resultSection",
        "resultTitleText",
    }
    missing = sorted(required_ids - ids)
    if missing:
        raise AssertionError(f"activity_main.xml is missing ids: {', '.join(missing)}")

    admin_ids = layout_ids(ADMIN_LAYOUT)
    required_admin_ids = {
        "adminLoginContainer",
        "adminLoginButton",
        "adminLogoutButton",
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
        ADMIN_ACTIVITY,
        [
            "loginContainer!.Visibility = Android.Views.ViewStates.Gone",
            "Administrator session ended.",
            "Latin and Cyrillic letters can look identical.",
        ],
    )

    assert_contains(
        MAIN_ACTIVITY,
        [
            '"/api/agent/bootstrap"',
            '"/api/agent/user-default-server"',
            '"/api/agent/domain-routes"',
            '"/api/agent/route-lookup?target=',
            '"autostart_enabled"',
            "EnrollDeviceAsync",
            "LoadUserUiAsync",
            "CheckUpdateAsync",
            "CudyAndroidUpdater.CheckAndDownloadAsync",
            "CudyAndroidUpdater.BeginInstall",
            "CudyUpdateJobService.Schedule",
            "typeof(AdminActivity)",
            'EnrollmentBootstrapUser = "cudy-enroll"',
            "EnrollmentBootstrapPort = 8766",
            "ReadEnrollmentBootstrapKey",
            'RequiredJsonString(provisioning, "ssh_private_key")',
            '"miui_autostart_confirmation_pending"',
            '"miui_autostart_confirmed"',
            "ConfirmMiuiAutostartIfPending",
            'dialog.SetPositiveButton("Enabled"',
            'SetPrimaryButton("Starting...", "#F2B134"',
            'SetPrimaryButton("Connected", "#1F9D55"',
            "StartUiRefreshLoop",
            "MaybeRecoverRequestedAgent",
            '"Downloading update {latestName}..."',
            '"Downloading {latestName}: {percent}%"',
            "If Play Protect blocks the update",
            "ShortUpdateError(updateError)",
            '"Update to {latestName}"',
            '" | Ready to install"',
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
        SSH_CONTROL,
        [
            "DownloadWithNewClient",
            "RangeHeaderValue(existingLength, null)",
            "FileMode.Append",
            "HttpCompletionOption.ResponseHeadersRead",
            'new AuthenticationHeaderValue("Bearer", token)',
        ],
    )
    assert_contains(
        UPDATER,
        [
            '"/api/agent/app-version?platform=android"',
            "DownloadWithNewClient",
            "HashMatches(tempPath, sha256)",
            "VerifyPackage(context, tempPath, versionCode)",
            "SignatureHashes(archive)",
            "PackageInstaller.SessionParams",
            "NotifyUpdateReady",
            "ActionInstallDownloadedUpdate",
            "CleanupInstalledUpdate",
            "ReconcileInstalledUpdate",
        ],
    )
    assert_contains(
        UPDATE_JOB,
        [
            ".SetPeriodic(6 * 60 * 60 * 1000L",
            ".SetRequiredNetworkType(NetworkType.Unmetered)",
            ".SetPersisted(true)",
            "force: parameters?.JobId == ImmediateJobId",
        ],
    )
    assert_contains(
        UPDATE_RECEIVER,
        [
            "PackageInstallStatus.PendingUserAction",
            "PackageInstallStatus.Success",
            "Intent.ExtraIntent",
            "Agent restart requested after successful update.",
            "CudyRecoveryJobService.Schedule(context)",
            "CudyAndroidUpdater.CleanupInstalledUpdate(context)",
            "Play Protect blocked the update.",
        ],
    )
    assert_contains(MANIFEST, ['android.permission.REQUEST_INSTALL_PACKAGES'])
    assert_contains(
        PROJECT,
        [
            "android_enrollment_bootstrap_ed25519",
            "EnsureEnrollmentBootstrapKey",
            "<ApplicationVersion>44</ApplicationVersion>",
            "<ApplicationDisplayVersion>1.43</ApplicationDisplayVersion>",
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
    if "Intent.ActionView" in main_text:
        raise AssertionError("MainActivity.cs must not send authenticated APK downloads to a browser")
    assert_contains(
        LAYOUT,
        [
            "updateVersionText",
            "Installed: - | Latest: not checked yet",
        ],
    )
    assert_contains(
        MAIN_ACTIVITY,
        [
            "LaunchMode = LaunchMode.SingleTask",
            "Task.Delay(TimeSpan.FromSeconds(2), token)",
            "Cudy Agent is up to date",
            'SetPositiveButton("OK"',
            "Installed version:",
            "Latest version:",
            "resultSection!.Visibility = ViewStates.Gone",
        ],
    )
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
            'PutBoolean("agent_requested_running", true)',
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
            "restart_vpn_restore_direct_temporarily",
            '"/api/agent/diagnostics"',
            "options.Inet4RouteRange",
            "AddTunDnsServers",
            "AddDisallowedApplication",
            "Added Android auto routes",
            "CudyAndroidProbeRunner.BuildLocalProbes(transportPlan)",
            "Duplicate start request ignored; control loop and TUN remain active",
            "StartFingerprint(",
            "app_version_name = installedAppVersion.Name",
            "app_version_code = installedAppVersion.Code",
            "InstalledAppVersion()",
            "ApplyAuthenticatedControlEndpoint(root)",
            'PutString("ssh_host", selectedHost)',
            'PutString("ssh_host_key_sha256", selectedHostKey)',
            "active SSH session is kept until reconnect",
            'StartString(intent, preferences, "control_url")',
            'GetBoolean("agent_requested_running", false)',
            'StoreLifecycleMarker("service_destroyed_unexpectedly"',
            'SaveServiceStatus("VPN engine stopped unexpectedly; retry pending", "restarting")',
            'TouchControlLoop("cycle-start")',
            'TouchControlLoop("cycle-complete")',
            'TouchControlLoop("cycle-error")',
            'RunWithControlHeartbeatAsync(',
            'TouchControlLoop(stage + "-running")',
            'TimeSpan.FromSeconds(ok ? 60 : 15)',
            'PutLong("control_loop_heartbeat_ms"',
            'PutLong("last_successful_control_ms"',
        ],
    )
    assert_contains(
        ROOT / "apps" / "CudyAndroidAgent" / "CudyApplication.cs",
        [
            "CudyAndroidUpdater.ReconcileInstalledUpdate(this)",
            "AndroidEnvironment.UnhandledExceptionRaiser",
            "AppDomain.CurrentDomain.UnhandledException",
            "TaskScheduler.UnobservedTaskException",
            'StoreCrash("android_unhandled"',
        ],
    )
    assert_contains(
        ROOT / "apps" / "CudyAndroidAgent" / "CudyRecoveryJobService.cs",
        [
            ".SetPersisted(true)",
            ".SetMinimumLatency(RecoveryDelayMilliseconds)",
            ".SetOverrideDeadline(RecoveryDeadlineMilliseconds)",
            "RecoveryJobIdA",
            "RecoveryJobIdB",
            "pendingA.IsPeriodic",
            "ScheduleNextJob",
            "_ = Task.Run(() => RunRecoveryJob(parameters));",
            "JobFinished(parameters, wantsReschedule: false);",
            "private const long RecoveryDelayMilliseconds = 30 * 1000;",
            'GetBoolean("agent_requested_running", false)',
            "StartForegroundService(intent)",
            'PutString("recovery_job_result", "start-requested")',
            "StalledControlLoopMilliseconds",
            "RecoverStalledProcess(preferences)",
            'PutString("service_status", "control loop stalled; process restart requested")',
            "Android.OS.Process.KillProcess(Android.OS.Process.MyPid())",
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
    if probe_text.count('"/api/agent/probe-jobs/result"') != 1:
        raise AssertionError("Each Android probe job must have one result-report path")
    assert_contains(
        ROOT / "apps" / "CudyAndroidAgent" / "CudyAndroidLibboxEngine.cs",
        [
            "MinimumReloadInterval",
            "libbox config reload deferred",
            "libbox pending config cancelled",
            "reload_deferred=",
            "MarkServiceStopped()",
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
            '["exclude_package"] = AndroidVpnBypassPackages(root)',
            '"com.nashvpn.cudyagent"',
            '"vpn_bypass_packages"',
            "is unavailable on Android and will be blocked",
            'outboundTags[entry.ServerId] = "block"',
        ],
    )
    assert_contains(
        SSH_CONTROL,
        [
            "Timeout = TimeSpan.FromSeconds(12)",
            "ConnectTimeout = TimeSpan.FromSeconds(2)",
            "Timeout = TimeSpan.FromSeconds(60)",
            "IsLocalForwardStarting",
            "attempt < 5",
        ],
    )
    assert_contains(
        VPN_SERVICE,
        [
            "TryStartCachedTransport()",
            'Path.Combine(filesPath, "transports", "cudy0.json")',
            "connected with cached routing; refreshing policy",
            "forceReload: cachedTransportNeedsRefresh",
            "Monitor.TryEnter(sshRequestLock, TimeSpan.FromMilliseconds(250))",
            "SSH request is still active during shutdown; cleanup continues in background.",
        ],
    )
    assert_contains(
        UPDATE_JOB,
        [
            "Task.Run(() => RunAsync(parameters, cancellationToken))",
        ],
    )
    print("Android agent UI static smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
