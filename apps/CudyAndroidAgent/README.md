# Cudy Android Agent

Native .NET Android prototype for the managed client.

Current MVP:

- stores control URL, device id, and device token;
- can reach the control-server through SSH remote `curl` with SSH.NET;
- fetches `/api/agent/config`;
- requests Android VPN permission and resumes start after the user accepts it;
- starts a foreground `VpnService`;
- periodically fetches control policy and posts `/api/agent/status`;
- shows service, policy, probe, route, transport, engine, runtime, and last
  error status on the main screen;
- shows battery, VPN permission, and MIUI Autostart readiness on the main
  screen;
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
- keeps `final=direct`, so only matching policy rules use provider exits;
- adds control-server `ip_routes` into Android `VpnService.Builder`, so Android
  system routing sends those CIDRs into the app TUN.
- claims and completes control-server probe jobs through local mixed proxy
  inbounds;
- registers a boot/reconnect receiver for `BOOT_COMPLETED`,
  `MY_PACKAGE_REPLACED`, and the smoke-test action
  `com.nashvpn.cudyagent.TEST_BOOT_START`.
- shows a first-run background permissions prompt and a `Setup permissions`
  button for battery optimization and MIUI Autostart setup.
- guides setup through notification permission, Android VPN permission,
  unrestricted battery mode, and MIUI Autostart/app settings.

Next implementation steps:

- verify behavior after a real phone reboot on each target Android/MIUI build.
- add a simple uninstall/reset helper for test devices.

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
build\releases\NashVPN-CudyAgent-android-arm64-v1.0-YYYYMMDD.apk
```

Manual smoke test:

1. Install the signed APK.
2. Enter `Control URL`, `Device ID`, device token, SSH host, SSH user, and SSH private key.
3. Tap `Save`, then `Setup permissions`.
4. Allow notification, VPN, and battery unrestricted mode when prompted.
5. On MIUI/Xiaomi/POCO/Redmi, enable Autostart when the app opens the vendor
   settings screen. Android does not allow the app to grant this vendor
   permission by itself.
6. Tap `Check control`.
7. Tap `Fetch policy`.
8. Tap `Prepare VPN` and grant Android VPN permission.
9. Tap `Start agent`.

Expected MVP status:

```text
service: ok ip=<n> cleanup=<n> transports=<n> prepared=1 stored=1 libbox=<version-or-state> config=<ok-or-error> engine=running server=android-unified iface=cudy0
```

Automated smoke test:

```powershell
powershell -ExecutionPolicy Bypass -File tools\android-agent-smoke.ps1 -StartEngine -WaitSeconds 35
```

If the phone shows the Android VPN permission dialog, unlock it and confirm.

Status/reset helper for test devices:

```powershell
powershell -ExecutionPolicy Bypass -File tools\android-agent-reset.ps1 -Status
powershell -ExecutionPolicy Bypass -File tools\android-agent-reset.ps1 -ForceStop
powershell -ExecutionPolicy Bypass -File tools\android-agent-reset.ps1 -ClearData
powershell -ExecutionPolicy Bypass -File tools\android-agent-reset.ps1 -Uninstall
```

Current test defaults:

```text
Control URL: http://127.0.0.1:18765
Device ID: isasha_X7Pro_Cudy-android
SSH host: 95.182.91.203
SSH user: cudy-tunnel-windows
```

If SSH host/user/key are filled, `Check control`, `Fetch policy`, and the
foreground service use SSH remote `curl` to talk to the control-server local
HTTP API on uswest. The `Control URL` field remains useful for a direct HTTP
test path.

The Android device token is generated separately on the control-server. The
test token and the matching SSH private key are kept under `secrets/agents/` in
the local workspace, not embedded into the APK.
