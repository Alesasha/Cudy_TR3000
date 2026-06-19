# Cudy Android Agent

Native .NET Android prototype for the managed client.

Current MVP:

- stores control URL, device id, and device token;
- can reach the control-server through SSH remote `curl` with SSH.NET;
- fetches `/api/agent/config`;
- requests Android VPN permission and resumes start after the user accepts it;
- starts a foreground `VpnService`;
- periodically fetches control policy and posts `/api/agent/status`;
- shows the last service status and last fetched policy summary on the main screen;
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

Next implementation step:

- report unified config/rule summary in `/api/agent/status`;
- harden probe jobs for mobile foreground/battery constraints;
- add boot/reconnect handling.

Safety note:

- The unified Android config uses `auto_route=true`, but `final=direct`.
- Provider exits are used only by generated `ip_routes`/`domain_routes` rules.

Build:

```powershell
dotnet build apps\CudyAndroidAgent\CudyAndroidAgent.csproj -c Release -p:RuntimeIdentifier=android-arm64
```

Rebuild `libbox.aar` and the arm64 APK:

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_android_libbox.ps1
```

Release APK:

```text
apps\CudyAndroidAgent\bin\Release\net10.0-android\android-arm64\com.nashvpn.cudyagent-Signed.apk
```

Manual smoke test:

1. Install the signed APK.
2. Enter `Control URL`, `Device ID`, device token, SSH host, SSH user, and SSH private key.
3. Tap `Save`, then `Check control`.
4. Tap `Fetch policy`.
5. Tap `Prepare VPN` and grant Android VPN permission.
6. Tap `Start agent`.

Expected MVP status:

```text
service: ok ip=<n> cleanup=<n> transports=<n> prepared=1 stored=1 libbox=<version-or-state> config=<ok-or-error> engine=running server=android-unified iface=cudy0
```

Automated smoke test:

```powershell
powershell -ExecutionPolicy Bypass -File tools\android-agent-smoke.ps1 -StartEngine -WaitSeconds 35
```

If the phone shows the Android VPN permission dialog, unlock it and confirm.

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
