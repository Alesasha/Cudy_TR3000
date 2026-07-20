# Android Agent

The Android agent is a native .NET Android client for managed control-plane
routing. It uses Android `VpnService`, SSH.NET for the control channel, and the
libbox/sing-box runtime for provider transports.

## Current Status

Verified on the physical test phone:

- installs and starts as `com.nashvpn.cudyagent`;
- stores control URL, device id, device token, SSH host/user/private key;
- reaches the uswest control-server through a restricted per-device SSH local
  forward;
- activates a fresh installation with one one-time code entered in the app;
- reaches the enrollment-only API through the shared, restricted `cudy-enroll`
  SSH account, then replaces that bootstrap credential with a unique
  per-device SSH key and token;
- pins the uswest Ed25519 host-key fingerprint before sending enrollment or
  device credentials;
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
- shows only the connection state and primary controls initially; routing,
  diagnostics, permissions, and technical settings are collapsed until asked
  for.
- renders Start as yellow while starting, green while connected, orange while
  recovering/degraded, and keeps Stop as a separate explicit action.
- reloads persisted start settings after Android sticky-service restarts and
  uses chained persisted recovery jobs with a 2-4 minute window as a second
  delayed recovery path.
- records boot, process, service lifecycle, native-engine stop, and recovery
  markers locally and reports lifecycle/recovery markers with agent status.
- guides first-run setup through notification permission, Android VPN
  permission, battery optimization exemption, and MIUI Autostart/app settings.

Latest published release candidate: `1.34 (35)`.

```text
ok engine=running server=android-unified iface=cudy0 vpn=validated probe_jobs jobs=1 completed=1 failed=0
```

Published release artifact:

- artifact: `build/releases/NashVPN-CudyAgent-android-arm64-v1.34-20260720.apk`;
- SHA256: `3bf023c53a35d4738f4b1beab1ba7800de1f6698061f8b311e00a70cc92916ff`;
- the production update manifest and APK have the same SHA256;
- the production bootstrap and issued per-device SSH channels passed an
  end-to-end test. Version 1.29 passed physical reboot, manual stop/start,
  process-kill recovery, foreground-service, VPN validation, and compact-UI
  acceptance on the Xiaomi Mi Note 10 Lite. Version 1.33 added authenticated,
  verified Android self-update delivery; version 1.34 adds the persistent
  installed/latest version summary and acknowledged update-result dialog.

Android update acceptance on 2026-07-20:

- a persisted unmetered-network job checks production every six hours;
- a real background job downloaded `1.32 (33)` through the enrolled device's
  SSH channel while `CudyVpnService` remained foreground and connected;
- the agent verified the manifest SHA256, package id, version code and signer,
  then displayed the dedicated update notification;
- the notification opened Android's unknown-source permission and
  `PackageInstaller` confirmation flow; Android completed the update from the
  test `1.31` package to `1.32`;
- final `1.34 (35)` was installed, package replacement restarted the VPN and
  control loop, and the immediate update job reported `up-to-date`;
- Android/MIUI still requires user approval for the final sideload install.

The previous 1.24 runtime smoke on the physical phone confirmed that:

- foreground service stayed running;
- Android VPN was established on `tun0` and reported `VALIDATED`;
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
- managed domain routes use selective FakeIP DNS, so HTTP provider exits receive
  the original host name instead of an already resolved destination IP; Direct
  domains continue to use normal DNS;
- changing Auto/probe policy is coalesced and libbox reloads no more than once
  per ten minutes, instead of recreating Android TUN on every control cycle;
- duplicate starts from package replacement, boot receiver, or the activity are
  ignored while the same control loop is already active;
- one unsupported transport no longer prevents supported Android transports
  from starting; routes assigned to it fail closed until a supported winner is
  selected;
- boot/reconnect receiver is registered for `BOOT_COMPLETED` and
  `MY_PACKAGE_REPLACED`;
- receiver start path was verified through the explicit test broadcast
  `com.nashvpn.cudyagent.TEST_BOOT_START`.
- the app now shows a first-run background permissions prompt and a
  `Setup permissions` button.
- the setup flow requests standard Android permissions directly where Android
  allows it, then opens vendor/app settings for the remaining MIUI-specific
  switch.

Code-only enrollment verification on 2026-07-18:

- the universal APK contains a shared bootstrap key for the shell-less
  `cudy-enroll` account;
- sshd restricts that account to local forwarding to `127.0.0.1:8766`; the
  listener exposes only `/healthz` and `/api/agent/enroll` and rate-limits
  enrollment attempts;
- a valid one-time code atomically creates the device, token and unique
  Ed25519 key, disables the code, and returns the private device credential
  once;
- the issued key was verified against the ordinary shell-less
  `cudy-tunnel-agent` account, restricted to `127.0.0.1:8765`;
- code reuse is rejected, and disabling/deleting the device removes its key
  from effective SSH access.

Real reboot check on the MIUI test phone:

- `LOCKED_BOOT_COMPLETED` was handled without reading credential-encrypted
  preferences;
- `BOOT_COMPLETED` started the foreground service after the configured network
  delay;
- SSH control, policy fetch and TUN recovered automatically;
- Android marked the VPN network `VALIDATED`.

Latest full-TUN verification on 2026-07-17:

- Android VPN interface `tun0` owns `0.0.0.0/0` and DNS `172.40.0.2`;
- Android reports the VPN network as `VALIDATED`;
- `mail.ru` matched the final `Direct` outbound;
- `chatgpt.com` received FakeIP and matched `out-proxyde` with the original
  `chatgpt.com:443` destination;
- `gemini.google.com` and `www.reuters.com` returned through managed provider
  exits, while `ozon.ru` and `gosuslugi.ru` remained Direct;
- Telegram `149.154.160.0/20` matched `out-proxyfr` by CIDR;
- five unchanged policy cycles logged the same config hash and did not reload
  libbox after one coalesced Auto-policy update.

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
build/releases/NashVPN-CudyAgent-android-arm64-v1.34-YYYYMMDD.apk
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

For an intermittent crash/reboot investigation, leave the phone connected and
run the non-invasive soak monitor. It samples the foreground service, recovery
job, package stopped state, and VPN network, then saves normal and crash logcat
buffers even when the script is interrupted:

```powershell
powershell -ExecutionPolicy Bypass -File tools\android-agent-soak.ps1 `
  -DurationMinutes 240 `
  -SampleSeconds 30
```

Results are written under `build/android-soak/<timestamp>-<serial>/` and contain
no provisioning token or private SSH key.

For Release APKs Android normally denies `run-as`; the script tolerates that and
uses logcat/dumpsys diagnostics instead.

## User Installation And Provisioning

The APK is universal for all users. Its shared bootstrap key cannot reach the
normal control API or admin UI; it can only forward to the enrollment listener.

1. The administrator either opens `Administration` in an already provisioned
   Android app or opens `http://127.0.0.1:18765/admin` through the operator SSH
   tunnel.
2. In `Agents`, select the user, enter the device id/name and keep platform
   `android`.
3. Click `Create one-time code`.
4. Send the current Android APK and the displayed one-time code to the intended
   user.
5. The user installs the APK, enters the code under `Activation`, and taps
   `Activate this device`.
6. Cudy Agent pins the SSH host key, consumes the code through the isolated
   enrollment listener, stores the returned token and unique device key, and
   uses only those individual credentials afterward.

If a code is sent to the wrong person, revoke it before it is used and create a
new one. Used and expired codes cannot activate another device.

## Mobile Administration

Android `1.34 (35)` contains a minimal protected administrator screen. Open
`Cudy Agent -> Administration`, enter an enabled administrator account and use
the following operations:

- list, create and edit users, including agent-only users without a web login;
- enable/disable users and replace a user's password;
- delete users with confirmation;
- list, enable/disable and permanently delete devices;
- create Android/Windows/Linux one-time device codes;
- display and share the one-time activation code.

The administrator password is never stored by the app. The screen reuses the
active agent SSH connection and falls back to a temporary per-device restricted
connection only when the agent service is off. Closing the screen destroys the
HTTP session. The web admin remains private and is not exposed as public HTTP.

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
Permissions: notifications=ok|needs allow; battery=ok|needs setup; vpn=ok|needs allow; autostart=confirmed|needs confirmation|n/a
Setup: allow notifications -> allow VPN -> allow unrestricted battery -> enable MIUI Autostart
```

It can request notification permission, Android VPN permission, standard Android
battery optimization exemption, and can open the MIUI Autostart screen. Android
does not let a third-party app enable MIUI Autostart automatically or query that
vendor-only permission afterward. When the user returns from the MIUI screen,
the app asks for an explicit confirmation and stores it instead of reporting a
permanent false warning. Use the in-app `Setup permissions` button first.
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
SSH user: cudy-tunnel-agent (new provisioned devices)
```

Legacy test settings may still be sourced from local secrets under:

```text
secrets/agents/isasha_X7Pro_Cudy-android/
```

Production device tokens and private keys are imported from the one-time
provisioning bundle and stored in Android private app preferences. They are not
embedded into the APK and must not be committed.

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
- Soak the explicit MIUI Autostart confirmation on the two enrolled phones.
- Add broader Android-device smoke coverage outside the current MIUI phone.
- Add optional rendered probes for services whose geographic decision is made
  by JavaScript rather than the initial HTTP body.
- Repeat the physical 1.34 reboot/process-kill/update acceptance on the second
  enrolled phone.

See also: [Android libbox runtime](android-libbox-runtime.md).
