# Cudy TR3000 VPN Routing Hub

Control plane and client agents for policy-based VPN/proxy routing.

The project started as a Cudy TR3000/OpenWrt router automation repo. The current
direction is a public control-server plus managed agents for Windows, Android,
Linux, and Cudy fallback routing.

## Current Architecture

- `uswest` control-server runs `tools/vpn_control_app.py`.
- Agents fetch user/domain policy and transport plans from the control-server.
- Windows and Android agents can apply local routes and start provider exits.
- Cudy remains useful as a LAN-wide agent and emergency fallback control path.
- Provider exits include own AmneziaWG servers plus VPNtype/LokVPN sing-box
  transports.

## Repository Layout

- `apps/CudyAndroidAgent/` - native .NET Android agent using Android `VpnService`
  and libbox/sing-box runtime.
- `tools/vpn_control_app.py` - control-server, admin/user UI, agent API, Auto
  cache, probe jobs, provider refresh workers.
- `tools/agent-windows/` - Windows managed route agent scripts and diagnostics.
- `tools/agent-linux/` - Linux managed agent prototype and install helpers.
- `tools/route_agent.py` - shared managed route agent engine used by desktop
  scripts.
- `tools/clone_control_server.py` - disaster-recovery clone tool for moving the
  production control-server to a replacement VPS.
- `tools/backup_control_server.py` - online SQLite/control-server backup tool
  for disaster recovery archives.
- `tools/vpn_inventory.py` - inventory validation and provider/Cudy snapshots.
- `tools/awg_client_add.py` - AmneziaWG client creation/statistics utility.
- `openwrt/` - scripts deployed to Cudy/OpenWrt.
- `deploy/uswest/` - systemd/Caddy deployment templates for the public
  control-server.
- `config/vpn_inventory.json` - static server catalog.
- `docs/` - architecture, operations, security, verification, and platform
  notes.
- `secrets/` - local-only keys, tokens, provider credentials, client configs;
  ignored by git.

## Quick Checks

Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

Validate core Python code and inventory:

```powershell
python tools\vpn_inventory.py validate
python -m py_compile tools\vpn_control_app.py tools\vpn_inventory.py tools\route_agent.py tools\awg_client_add.py
```

Clone the production control-server to a replacement VPS:

```powershell
python tools\clone_control_server.py --target-host <new-vps-ip>
```

Create a local disaster-recovery backup of the live control-server:

```powershell
python tools\backup_control_server.py
```

Build the Android release APK:

```powershell
dotnet build apps\CudyAndroidAgent\CudyAndroidAgent.csproj -c Release -p:RuntimeIdentifier=android-arm64
```

Run the Android smoke test with a connected device:

```powershell
powershell -ExecutionPolicy Bypass -File tools\android-agent-smoke.ps1 -StartEngine -WaitSeconds 70 -ApkPath "C:\Users\Alexander\Cudy_TR3000\apps\CudyAndroidAgent\bin\Release\net10.0-android\android-arm64\com.nashvpn.cudyagent-Signed.apk"
```

## Documentation

Start here:

- [Architecture](docs/architecture.md)
- [Control server](docs/control-server.md)
- [Control app](docs/control-app.md)
- [Operations](docs/operations.md)
- [Verification](docs/verification.md)
- [Android agent](docs/android-agent.md)
- [Android libbox runtime](docs/android-libbox-runtime.md)
- [Windows managed transport POC](docs/windows-managed-transport-poc.md)
- [Security](docs/security.md)

Historical working notes are kept in `MAIN.md` and `BRANCH-*.md`. They are useful
for context but should not be treated as current operating instructions.

## Security Rules

- Do not commit `secrets/`, `.env`, local SQLite databases, APKs, build outputs,
  or provider subscription data.
- Keep real provider/API credentials only in environment variables or ignored
  files under `secrets/`.
- Treat any committed credential as compromised and rotate it.
