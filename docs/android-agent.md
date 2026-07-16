# Android Agent

The Android agent is a native .NET Android client for managed control-plane
routing. It uses Android `VpnService`, SSH.NET for the control channel, and the
libbox/sing-box runtime for provider transports.

## Current Status

Verified on the physical test phone:

- installs and starts as `com.nashvpn.cudyagent`;
- stores control URL, device id, device token, SSH host/user/private key;
- reaches the uswest control-server through SSH remote `curl`;
- fetches `/api/agent/config`;
- parses `transport_plan`, `ip_routes`, and `domain_routes`;
- builds one unified Android sing-box config;
- starts a foreground `VpnService`;
- starts libbox behind Android TUN;
- captures full IPv4 traffic in Android `VpnService.Builder`, including DNS;
- excludes the agent package from its own VPN and protects libbox sockets so
  direct/provider/control connections use the physical default network;
- posts `/api/agent/status`;
- can run local candidate probes through generated local mixed proxy inbounds.
- shows service, policy, probe, route, transport, engine, runtime, and last
  error status on the main screen.
- shows `battery`, `vpn`, and MIUI `autostart` readiness on the main screen.
- guides first-run setup through notification permission, Android VPN
  permission, battery optimization exemption, and MIUI Autostart/app settings.

Latest verified release: `1.21 (22)`.

```text
ok engine=running server=android-unified iface=cudy0 vpn=validated probe_jobs jobs=1 completed=1 failed=0
```

Latest Release APK smoke on the physical phone:

- artifact: `build/releases/NashVPN-CudyAgent-android-arm64-v1.21-20260716.apk`;
- SHA256: `2846b6385e4c0117e1cfdac050d925f56efa4643bdf46d3497bd556b998fc977`;
- foreground service stayed running;
- Android VPN was established on `tun2`;
- control-server reported `isasha_X7Pro_Cudy-android` online and healthy;
- a production probe job tested `proxyde` and `proxynl` while active browser
  traffic continued through the VPN;
- probe jobs use persistent loopback-only mixed inbounds and do not reload or
  interrupt the active TUN;
- transports used by recent Android probe jobs stay warm for six hours (up to
  64 logical servers), so completing one probe and starting another does not
  continuously change the unified config or recreate the Android VPN; only
  libbox-compatible transport types are retained, while native AWG remains a
  separate final project phase;
- boot/reconnect receiver is registered for `BOOT_COMPLETED` and
  `MY_PACKAGE_REPLACED`;
- receiver start path was verified through the explicit test broadcast
  `com.nashvpn.cudyagent.TEST_BOOT_START`.
- the app now shows a first-run background permissions prompt and a
  `Setup permissions` button.
- the setup flow requests standard Android permissions directly where Android
  allows it, then opens vendor/app settings for the remaining MIUI-specific
  switch.

Real reboot check on the MIUI test phone:

- `LOCKED_BOOT_COMPLETED` was handled without reading credential-encrypted
  preferences;
- `BOOT_COMPLETED` started the foreground service after the configured network
  delay;
- SSH control, policy fetch and TUN recovered automatically;
- Android marked the VPN network `VALIDATED`.

Latest full-TUN verification on 2026-07-16:

- Android VPN interface `tun0` owns `0.0.0.0/0` and DNS `172.40.0.2`;
- Android reports the VPN network as `VALIDATED`;
- `mail.ru` matched the final `Direct` outbound;
- `chatgpt.com` matched `out-proxyde` by SNI;
- Telegram `149.154.160.0/20` matched `out-proxyfr` by CIDR;
- unchanged policy cycles logged the same config hash and did not reload
  libbox.

The control policy still includes these Telegram CIDRs:

```text
149.154.160.0/20
91.105.192.0/23
91.108.12.0/22
91.108.16.0/22
91.108.20.0/22
91.108.4.0/22
91.108.56.0/22
91.108.8.0/22
```

## Build

The repo pins the .NET SDK through `global.json`. Build and copy the release APK:

```powershell
powershell -ExecutionPolicy Bypass -File tools\Build-AndroidAgentRelease.ps1
```

Raw build output:

```text
apps/CudyAndroidAgent/bin/Release/net10.0-android/android-arm64/com.nashvpn.cudyagent-Signed.apk
```

The operator-friendly versioned copy is written to:

```text
build/releases/NashVPN-CudyAgent-android-arm64-v1.21-YYYYMMDD.apk
```

The current release profile intentionally keeps:

- `RunAOTCompilation=false`;
- `AndroidEnableMarshalMethods=false`;
- `AndroidLinkMode=None`;
- `AndroidIncludeDebugSymbols=true`.

This avoids a .NET Android marshal-methods crash seen with the default Release
profile on the current local toolchain.

## ADB Smoke Test

Connect a physical Android device with USB debugging enabled:

```powershell
powershell -ExecutionPolicy Bypass -File tools\android-agent-smoke.ps1 `
  -StartEngine `
  -WaitSeconds 70 `
  -ApkPath "C:\Users\Alexander\Cudy_TR3000\apps\CudyAndroidAgent\bin\Release\net10.0-android\android-arm64\com.nashvpn.cudyagent-Signed.apk" `
  -DebugProbeUrl "http://ifconfig.me/ip" `
  -DebugProbeCandidates "proxyde,proxynl"
```

The smoke script:

- installs the APK;
- injects local test settings from `secrets/agents/...`;
- starts the foreground VPN service;
- waits for the first control loop;
- prints service status, policy summary, device ping, and recent app logs.

For Release APKs Android normally denies `run-as`; the script tolerates that and
uses logcat/dumpsys diagnostics instead.

## ADB Status And Reset

For test devices, use the reset helper. Without destructive flags it only prints
status:

```powershell
powershell -ExecutionPolicy Bypass -File tools\android-agent-reset.ps1 -Status
```

Useful reset actions:

```powershell
powershell -ExecutionPolicy Bypass -File tools\android-agent-reset.ps1 -ForceStop
powershell -ExecutionPolicy Bypass -File tools\android-agent-reset.ps1 -ClearData
powershell -ExecutionPolicy Bypass -File tools\android-agent-reset.ps1 -Uninstall
```

`-ClearData` removes locally stored control URL, device id, token, SSH key, and
saved runtime status from the Android app. It does not revoke the control-server
device token; revoke that separately in the admin UI if the device is retired.

## Boot/Reconnect Smoke Test

The production receiver listens for Android boot and package-replaced events.
Android blocks manual `BOOT_COMPLETED` and `MY_PACKAGE_REPLACED` broadcasts from
ADB, so the app also exposes a test-only equivalent action:

```powershell
$adb = "C:\Program Files (x86)\Android\android-sdk\platform-tools\adb.exe"
& $adb -s f88d126d shell am broadcast `
  -a com.nashvpn.cudyagent.TEST_BOOT_START `
  -n com.nashvpn.cudyagent/com.nashvpn.cudyagent.BootReceiver
```

Expected logcat lines:

```text
Boot receiver requested agent start for com.nashvpn.cudyagent.TEST_BOOT_START.
Control loop ok ip=... transports=... engine=running ...
```

On MIUI and other aggressive Android builds, the user may still need to allow
autostart and disable battery restrictions for reliable background start after a
real device reboot.

The app shows a compact permissions line:

```text
Permissions: notifications=ok|needs allow; battery=ok|needs setup; vpn=ok|needs allow; autostart=check MIUI|n/a
Setup: allow notifications -> allow VPN -> allow unrestricted battery -> enable MIUI Autostart
```

It can request notification permission, Android VPN permission, standard Android
battery optimization exemption, and can open the MIUI Autostart screen. Android
does not let a third-party app enable MIUI Autostart automatically. Use the
in-app `Setup permissions` button first.
If vendor settings do not open automatically, use these paths:

```text
Security -> Manage apps -> Cudy Agent -> Autostart: on
Settings -> Battery -> Battery saver -> Cudy Agent -> No restrictions
Recent apps -> Cudy Agent -> Lock, if available
```

## Test Defaults

```text
Device ID: isasha_X7Pro_Cudy-android
Control URL: http://127.0.0.1:18765
SSH host: 95.182.91.203
SSH user: cudy-tunnel-windows
```

The device token and SSH private key are local secrets under:

```text
secrets/agents/isasha_X7Pro_Cudy-android/
```

They are not embedded into the APK and must not be committed.

## Control-Server Integration

Android can report these effective capabilities:

- route policy CIDRs through Android `VpnService`;
- manage sing-box/libbox transports supplied by `transport_plan`;
- run local candidate probes through temporary local mixed proxy inbounds;
- claim and complete probe jobs when assigned by the control-server.

The control-server should still treat Android as a foreground/mobile agent:

- do not require it to be online all the time;
- keep probe jobs short;
- expect Android battery/foreground-service restrictions;
- prefer Windows/Linux/Cudy agents for long-running background checks.

## Remaining Work

- Run a longer locked-screen/background soak and verify mobile-data/Wi-Fi
  transitions.
- Resolve or clearly explain the remaining Android Doze whitelist warning.
- Add broader Android-device smoke coverage outside the current MIUI phone.
- Add optional rendered probes for services whose geographic decision is made
  by JavaScript rather than the initial HTTP body.
- Simplify the main UI and move technical diagnostics to a dedicated view.

See also: [Android libbox runtime](android-libbox-runtime.md).
