# Android libbox Runtime

The Android agent uses the same runtime direction as SFA/sing-box for Android:
`libbox.aar` exposes `io.nekohasekai.libbox.*`, and the Android app provides
platform-specific `VpnService`/TUN integration.

Current repo status:

- `CudySingBoxConfig` builds sing-box JSON from control-server `transport_plan`.
- `CudyTransportStore` writes those JSON files into app-private
  `files/transports/`.
- `CudySingBoxRuntime` calls the generated .NET binding for
  `IO.Nekohasekai.Libbox.Libbox`.
- `CudyAndroidLibboxEngine` calls `Libbox.setup(...)`, owns
  `libbox.CommandServer`, and starts/reloads the first stored transport config.
- `CudyLibboxPlatform` implements the Android callbacks required by libbox,
  including `PlatformInterface.openTun(...)` through our `VpnService`.
- Android now writes a unified sing-box config from the whole control-server
  policy: one TUN inbound, all `transport_plan` outbounds, policy
  `ip_routes`/`domain_routes`, and `final=direct`.
- With `apps/CudyAndroidAgent/Libs/libbox.aar`, the smoke test can load
  `lib/arm64-v8a/libbox.so` and call `Libbox.checkConfig(...)` for the first
  stored config.
- Current verified release status on the test phone:
  `ok ip=8 cleanup=0 transports=2 prepared=1 stored=1 libbox=unknown config=ok engine=running server=android-unified iface=cudy0`.
- Android `VpnService.Builder` uses libbox `Inet4RouteRange` and captures
  `0.0.0.0/0`; DNS is sent to the TUN and hijacked by sing-box.
- The agent package is excluded from the VPN, while libbox
  `auto_detect_interface` invokes the Android protect callback for direct and
  provider sockets. This prevents full-TUN recursion.
- With the unified Android VPN running, Android reports a validated `tun0`.
  Live logs prove `example.com -> direct`, `chatgpt.com -> out-proxynl`, and
  Telegram CIDRs -> `out-proxynl`.
- The engine hashes generated configs and skips reloads when the policy is
  unchanged.

Why the first iteration used a probe before a full runtime:

- The official SFA Android app depends on local files `app/libs/libbox.aar` and
  `app/libs/libbox-legacy.aar`; they are generated from `SagerNet/sing-box`, not
  committed into `sing-box-for-android`.
- The control loop, SSH control path, transport-plan parsing, and config
  generation were validated before the AAR was available.
- Now that the AAR is available, the app uses .NET Android's generated binding
  instead of reflection.

Observed local blockers and fixes:

- The Go MSI requires administrator rights for a normal install. We extract it
  locally with `msiexec /a` into `build/tools/go-msi-extract/Go`.
- The system Android SDK directory requires administrator rights for NDK
  installs. We install NDK/platform/build-tools into
  `build/tools/android-sdk`.
- `gomobile` on Windows can crash on hidden drive-current-directory environment
  variables such as `=E:=E:\`; `tools/build_android_libbox.ps1` runs the build
  through a sanitized environment.
- Some Google/GitHub download IPs may be routed by Cudy PBR through `awg2`; the
  build script can temporarily add current download IPs to
  `pbr_wan_4_dst_ip_user` before downloading.

Upstream references:

- `SagerNet/sing-box-for-android` uses `app/libs/libbox.aar`.
- `SagerNet/sing-box` Makefile has `lib_android` and `lib_android_new` targets.
- Official sing-box Android documentation describes SFA as the client that runs
  local/remote sing-box configs and provides Android TUN implementation.

Rebuild:

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_android_libbox.ps1
```

The script builds an arm64 test AAR and copies it to:

```text
apps/CudyAndroidAgent/Libs/libbox.aar
```

Release runtime smoke test:

```powershell
powershell -ExecutionPolicy Bypass -File tools\android-agent-smoke.ps1 -StartEngine -WaitSeconds 70 -ApkPath "C:\Users\Alexander\Cudy_TR3000\apps\CudyAndroidAgent\bin\Release\net10.0-android\android-arm64\com.nashvpn.cudyagent-Signed.apk"
```

If Android has not granted VPN permission yet, unlock the phone and accept the
standard VPN connection dialog. The app now records
`waiting for Android VPN permission` while it waits for that consent.

Remaining implementation work:

1. Publish and reboot-test the full-TUN build as a new release.
2. Extend Android status with active config summary: route count, outbound tags,
   and last probe result.
3. Harden probe-job scheduling for mobile foreground/battery constraints.
