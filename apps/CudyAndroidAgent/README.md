# Cudy Android Agent

Native .NET Android prototype for the managed client.

Current MVP:

- stores control URL, device id, and device token;
- can reach the control-server through SSH remote `curl` with SSH.NET;
- fetches `/api/agent/config`;
- requests Android VPN permission and resumes start after the user accepts it;
- starts a foreground `VpnService`;
- periodically fetches control policy and posts `/api/agent/status`;
- checks the effective control-server critical-service list in parallel,
  restores direct routing after three consecutive critical failures, and
  retries the VPN after a bounded recovery delay;
- keeps the main screen compact: a state-aware Start/Connected/Reconnecting
  control, Stop, autostart, update, activation, and collapsed routing,
  diagnostics, and advanced sections;
- records process, service lifecycle, boot receiver, and recovery-job markers
  for post-failure diagnostics;
- restores configuration after Android `START_STICKY` restarts and uses chained
  persisted recovery jobs with a 2-4 minute window as a second recovery path;
- activates a device through `/api/agent/enroll` with a one-time enrollment
  code, then stores the returned device token and unique per-device SSH key;
- uses the APK's shared `cudy-enroll` key only for the isolated enrollment
  listener on port `8766`; that account cannot reach the ordinary control API;
- lets an activated device call agent-scoped control APIs:
  `/api/agent/bootstrap`, `/api/agent/user-default-server`,
  `/api/agent/domain-routes`, `/api/agent/route-lookup`, and
  `/api/agent/app-version`;
- parses `transport_plan` and prepares compatible sing-box JSON in memory for
  supported transport types.
- stores prepared sing-box JSON files under app-private `files/transports/`.
- loads `apps/CudyAndroidAgent/Libs/libbox.aar` when present and checks the
  first generated sing-box config with `Libbox.checkConfig(...)`.
- starts `libbox.CommandServer` and reloads the first stored transport config
  through the generated .NET binding.
- implements the Android libbox `PlatformInterface.openTun(...)` callback.
- builds one unified Android sing-box config from all control-server
  `transport_plan`, `ip_routes`, and `domain_routes` entries.
- answers DNS for managed domain routes from a selective FakeIP range so HTTP
  proxy transports receive the original destination host name; Direct domains
  keep normal DNS answers.
- coalesces changing Auto/probe policy and limits disruptive libbox/TUN reloads
  to one per ten minutes.
- ignores duplicate service starts for an already active control configuration.
- keeps `final=direct`, so only matching policy rules use provider exits;
- adds control-server `ip_routes` into Android `VpnService.Builder`, so Android
  system routing sends those CIDRs into the app TUN.
- claims and completes control-server probe jobs through persistent loopback-only
  mixed proxy inbounds, without reloading the active TUN or interrupting user
  traffic;
- registers a boot/reconnect receiver for `LOCKED_BOOT_COMPLETED`,
  `BOOT_COMPLETED`, `USER_UNLOCKED`, `MY_PACKAGE_REPLACED`, and the smoke-test action
  `com.nashvpn.cudyagent.TEST_BOOT_START`.
- records `boot_receiver_*` markers in app-private status so reboot/autostart
  diagnostics can distinguish a missing vendor boot broadcast from a service
  start failure.
- shows a first-run background permissions prompt and a `Setup permissions`
  button for battery optimization and MIUI Autostart setup.
- guides setup through notification permission, Android VPN permission,
  unrestricted battery mode, and MIUI Autostart/app settings.
- checks the authenticated control-server update channel every six hours on an
  unmetered network, downloads a newer APK through the device's private SSH
  channel, verifies its SHA256, package id, version and signing certificate,
  and posts an install notification;

Next implementation steps:

- repeat the physical reboot acceptance on each additional target Android/MIUI
  build before broad rollout.
- add a simple uninstall/reset helper for test devices.
- add loop-free protected direct outbound for full domain/SNI capture without
  forcing ordinary traffic through a provider exit.

Current published release candidate is `1.34 (35)`. Version 1.30 passed physical
UI, sticky restart, crash/reboot and persisted recovery-job checks. Version 1.33
added authenticated, verified background APK download; version 1.34 keeps the
installed/latest versions on the main screen and makes manual check results wait
for explicit acknowledgement. The routing runtime inherited
from `1.24 (25)` has already passed policy fetch,
foreground-service, libbox, selective-routing, status, non-disruptive probe and
real-reboot checks.

Safety note:

- The unified Android config uses `auto_route=true`, but `final=direct`.
- Provider exits are used only by generated `ip_routes`/`domain_routes` rules.

Build:

```powershell
powershell -ExecutionPolicy Bypass -File tools\Build-AndroidAgentRelease.ps1
```

Rebuild `libbox.aar` and the arm64 APK:

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_android_libbox.ps1
```

Release APK:

```text
apps\CudyAndroidAgent\bin\Release\net10.0-android\android-arm64\com.nashvpn.cudyagent-Signed.apk
build\releases\NashVPN-CudyAgent-android-arm64-v1.34-YYYYMMDD.apk
```

Manual smoke test:

1. Install the signed APK.
2. Enter the one-time activation code and tap `Activate this device`. The app
   obtains and stores its individual SSH key and device token automatically.
3. Tap `Setup permissions`.
4. Allow notification, VPN, and battery unrestricted mode when prompted.
5. On MIUI/Xiaomi/POCO/Redmi, enable Autostart when the app opens the vendor
   settings screen. Android does not allow the app to grant this vendor
   permission by itself.
6. Tap `Load settings`.
7. Set the default server or a domain route if needed, then tap the matching
   save button.
8. Tap `Prepare VPN` and grant Android VPN permission.
9. Tap `Start`; it changes to yellow `Starting...` and then green
   `Connected` when the policy loop is healthy.

Expected MVP status:

```text
service: ok ip=<n> cleanup=<n> transports=<n> prepared=1 stored=1 libbox=<version-or-state> config=<ok-or-error> engine=running server=android-unified iface=cudy0
```

Automated smoke test:

```powershell
powershell -ExecutionPolicy Bypass -File tools\android-agent-smoke.ps1 -StartEngine -WaitSeconds 35
```

If the phone shows the Android VPN permission dialog, unlock it and confirm.

Reboot/autostart verification:

```powershell
powershell -ExecutionPolicy Bypass -File tools\android-agent-smoke.ps1 -Build -WaitSeconds 25
adb reboot
# wait until the phone has booted, then unlock the screen once
powershell -ExecutionPolicy Bypass -File tools\android-agent-smoke.ps1 -NoInstall -NoStart
```

After a successful real reboot, the `-NoStart` check should show:

```text
State: RUNNING_UNLOCKED
Process: <pid>
Service: CudyVpnService ... isForeground=true
boot_receiver_action: android.intent.action.USER_UNLOCKED
boot_receiver_result: start-requested
service_status: waiting for network after boot (45s)
service_status: ok ...
```

The boot receiver intentionally delays the first control-server fetch by 45
seconds after `BOOT_COMPLETED`/`USER_UNLOCKED`. On MIUI this avoids a noisy
first SSH timeout while Wi-Fi/cellular networking is still settling after
unlock.

If the APK exposes the receiver but no `boot_receiver_*` markers appear after
unlock, the Android vendor firmware most likely blocked the boot/unlock
broadcast. On MIUI/Xiaomi/POCO/Redmi, open `Setup permissions`, enable
Autostart for Cudy Agent, set Battery saver to No restrictions, and repeat the
reboot test. Android does not provide a standard API that lets the app grant
the MIUI Autostart permission by itself.

Status/reset helper for test devices:

```powershell
powershell -ExecutionPolicy Bypass -File tools\android-agent-reset.ps1 -Status
powershell -ExecutionPolicy Bypass -File tools\android-agent-reset.ps1 -ForceStop
powershell -ExecutionPolicy Bypass -File tools\android-agent-reset.ps1 -ClearData
powershell -ExecutionPolicy Bypass -File tools\android-agent-reset.ps1 -Uninstall
```

Fresh-install activation defaults:

```text
Control URL: empty
Device ID: empty
Device token: empty
Device SSH settings: empty
```

The APK contains only the restricted enrollment bootstrap. After the user
enters a one-time activation code, the app receives and stores its unique
device id, token, SSH key, host pin, and control URL. Subsequent control calls
use only those per-device credentials.

One-time Android enrollment code:

```powershell
python tools\vpn_control_app.py enrollment-create USER_ID --device-id USER_ID-android --platform android
```

The code is printed once. The Android app sends it to `/api/agent/enroll`,
receives a device token, saves it locally, and then uses only agent-scoped APIs.

Release update metadata is generated with:

```powershell
powershell -ExecutionPolicy Bypass -File tools\Build-AgentUpdateArtifacts.ps1 `
  -Platforms android `
  -AndroidApk build\releases\NashVPN-CudyAgent-android-arm64-v1.34-YYYYMMDD.apk
```

Android cannot silently replace a side-loaded APK without Play Store, MDM, or
root. Version 1.34 checks every six hours on an unmetered network, downloads the
APK over the authenticated per-device SSH channel, and verifies SHA256,
`com.nashvpn.cudyagent`, version code, and the installed signing certificate.
It then shows a notification. Tapping it opens the Android package installer;
the user must approve the final installation, and MIUI may require the one-time
`Install unknown apps` permission. Builds older than 1.33 need one manual current
release installation before they can use this update path.
