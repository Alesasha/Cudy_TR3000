# Cudy Android Agent

Native .NET Android prototype for the managed client.

Current MVP:

- stores control URL, device id, and device token;
- can reach the control-server through SSH remote `curl` with SSH.NET;
- fetches `/api/agent/config`;
- requests Android VPN permission and resumes start after the user accepts it;
- starts a foreground `VpnService`;
- periodically fetches control policy and posts `/api/agent/status`;
- checks the effective control-server critical-service list in parallel and
  restores direct routing after three consecutive critical failures;
- shows service, policy, probe, route, transport, engine, runtime, and last
  error status on the main screen;
- shows battery, VPN permission, and MIUI Autostart readiness on the main
  screen;
- has a production control screen with ON/OFF, status refresh, autostart
  checkbox, one-time activation code, default server, domain route, route
  lookup, and update check controls;
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

Next implementation steps:

- repeat the physical reboot acceptance on each additional target Android/MIUI
  build before broad rollout.
- add a simple uninstall/reset helper for test devices.
- add loop-free protected direct outbound for full domain/SNI capture without
  forcing ordinary traffic through a provider exit.

Current published release is `1.26 (27)`. The control-server update manifest
contains the matching APK. Its code-only bootstrap and issued per-device SSH
channel passed an end-to-end production test. Version 1.25 is accepted on two
physical phones; 1.26 is the pending in-place UI/permission update. The routing
runtime inherited from `1.24 (25)` has already passed policy fetch,
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
build\releases\NashVPN-CudyAgent-android-arm64-v1.26-YYYYMMDD.apk
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
9. Tap `ON`.

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

Release update metadata:

```powershell
$env:CUDY_ANDROID_VERSION_NAME = "1.0"
$env:CUDY_ANDROID_VERSION_CODE = "1"
$env:CUDY_ANDROID_APK_URL = "https://example.invalid/NashVPN-CudyAgent.apk"
```

Android cannot silently replace a side-loaded APK without Play Store, MDM, or
root. `Check for updates` shows the available version and opens
`CUDY_ANDROID_APK_URL` when the server reports a newer version code.
