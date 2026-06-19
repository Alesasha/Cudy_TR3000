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
- adds control-server `ip_routes` into Android `VpnService.Builder`;
- posts `/api/agent/status`;
- can run local candidate probes through generated local mixed proxy inbounds.

Latest verified smoke status:

```text
ok ip=8 cleanup=0 transports=2 prepared=1 stored=1 libbox=unknown config=ok engine=running server=android-unified iface=cudy0 probe_jobs jobs=0 completed=0 failed=0
```

Latest verified Android VPN routes included the Telegram CIDRs:

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

The repo pins the .NET SDK through `global.json`. Build the release APK:

```powershell
dotnet build apps\CudyAndroidAgent\CudyAndroidAgent.csproj -c Release -p:RuntimeIdentifier=android-arm64
```

Release output:

```text
apps/CudyAndroidAgent/bin/Release/net10.0-android/android-arm64/com.nashvpn.cudyagent-Signed.apk
```

The current release profile intentionally keeps:

- `RunAOTCompilation=false`;
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

- Add a user-facing status screen with active route count, active transports,
  last probe winner, and last control-server error.
- Add Android boot/reconnect handling after device restart.
- Add battery optimization instructions or an in-app warning.
- Package release APKs with versioned filenames.
- Add a simple uninstall/reset procedure for test devices.

See also: [Android libbox runtime](android-libbox-runtime.md).
