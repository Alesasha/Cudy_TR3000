# Cudy Go Runtime

This document defines the first safe Go step for Cudy/OpenWrt.

The goal is not to replace the full Python control-server in one jump. The first
Go binary should replace only the lightweight Cudy fallback layer while keeping
the current agent API and control-state artifacts compatible.

## Current Target

`cmd/cudy-fallback` is a compact HTTP service for Cudy. It reads the files that
are already synced to the router:

```text
/www/cudy-control/endpoints.json
/www/cudy-control/state.json
```

It serves:

```text
GET /healthz
GET /readyz
GET /api/control/endpoints
GET /api/cudy/runtime
GET /api/cudy/agent-preview
GET /cudy-control/endpoints.json
GET /cudy-control/state.json
```

This lets agents keep using the same fallback discovery contract. It also gives
the router a local readiness check before it grows into a fuller fallback
control-server.

## Build

The local Go toolchain can be unpacked under:

```text
C:\Users\Alexander\sdk\go1.26.4\Go\bin\go.exe
```

Build and test the OpenWrt artifact:

```powershell
powershell -ExecutionPolicy Bypass -File tools\Build-CudyFallbackGo.ps1
```

The default output is:

```text
build\cudy\cudy-fallback-linux-arm64
```

The default `GOARCH=arm64` matches the expected Cudy TR3000/OpenWrt target. If
the live router reports a different architecture, rebuild with `-GoArch`.

Deploy the current build to Cudy:

```powershell
python tools\deploy_cudy_go_fallback.py --dry-run
python tools\deploy_cudy_go_fallback.py
python tools\check_cudy_go_fallback.py --strict
```

The deploy helper uses SSH exec upload instead of SFTP because the current Cudy
OpenWrt image does not expose a working SFTP subsystem.

## OpenWrt Service

The init template is:

```text
openwrt/cudy-fallback.init
```

Planned install path:

```text
/usr/bin/cudy-fallback
/etc/init.d/cudy-fallback
```

The service listens only on loopback by default:

```text
127.0.0.1:8765
```

Initial deployment on the current Cudy is verified:

```text
arch=aarch64
target=mediatek/filogic
service=cudy-fallback running
/healthz ok=true
/readyz ok=true
```

`/api/cudy/runtime` is read-only. It collects a compact live snapshot from
OpenWrt without applying routes or restarting services:

- architecture and OpenWrt target;
- PBR supported interfaces and current target interface;
- network links and IPv4 addresses;
- status of `cudy-fallback`, PBR, and sing-box provider services;
- root cron entries;
- TCP listeners.

`/api/cudy/agent-preview` is also read-only. When
`/etc/cudy-fallback/agent.json` exists, it fetches `/api/agent/config` from the
configured control-server and returns only a sanitized Cudy applicability
preview:

- `transport_plan` entries as `server_id`, `interface`, `transport_type`, and
  whether the interface is present and PBR-supported on Cudy;
- `domain_routes` and `ip_routes` as targets plus the interface Cudy would use;
- warnings for missing targets, missing interface mappings, absent interfaces,
  or interfaces not listed in `pbr.config.supported_interface`.

The endpoint deliberately does not return raw transport configs, provider
credentials, tokens, or route-apply commands. It does not modify routes, PBR,
or sing-box services.

Minimal agent settings file:

```json
{
  "control_url": "http://127.0.0.1:18765",
  "agent_config_path": "/api/agent/config",
  "device_id": "cudy-home",
  "token_file": "/etc/cudy-fallback/agent.token"
}
```

Keep both files readable only by root. Until the later SSH-control-tunnel step,
`control_url` must already be reachable from Cudy.

Install these settings with the helper, after creating a dedicated Cudy device
token on the control-server:

```powershell
python tools\vpn_control_app.py service-user-create cudy_lan --display-name "Cudy LAN Agent"
python tools\vpn_control_app.py device-create cudy_lan --device-id cudy-home --display-name "Cudy Home Router" --platform other --json
$env:CUDY_AGENT_TOKEN = "<dedicated-cudy-device-token>"
python tools\install_cudy_agent_settings.py --dry-run
python tools\install_cudy_agent_settings.py
Remove-Item Env:CUDY_AGENT_TOKEN
```

Run the `vpn_control_app.py` commands against the production control-server DB
when preparing the real Cudy device. The local DB is useful only for regression
tests.

The helper never prints the token and stores it separately from `agent.json` as
`/etc/cudy-fallback/agent.token`.

The optional control-tunnel service is prepared separately. It uses the
Dropbear/OpenWrt `ssh` client to keep:

```text
127.0.0.1:18765 -> <control-user>@<uswest>:127.0.0.1:8765
```

Install it only after the corresponding public key is authorized for the
restricted tunnel user on the control-server:

```powershell
python tools\install_cudy_control_tunnel.py --dry-run
python tools\install_cudy_control_tunnel.py `
  --identity-file secrets\cudy-control-tunnel\control_tunnel_ed25519 `
  --enable --start
```

This tunnel still does not apply routes; it only makes the control API
reachable for `/api/cudy/agent-preview`.

Public/static access can still be provided by uhttpd serving `/www/cudy-control`
or by a controlled reverse proxy rule later. Do not expose a broader fallback
API on WAN until authentication and state-restore behavior are explicitly
implemented.

## Readiness Rules

`/readyz` is OK only when:

- `endpoints.json` exists;
- it contains at least one endpoint;
- `valid_until` is in the future;
- `state.json` exists;
- `created_at` is not stale;
- archive metadata is complete: `archive_name`, `sha256`, `bytes`.

The default state age limit is `3h`, matching the current fallback freshness
check window.

## Next Go Steps

After the fallback service is deployed and stable:

1. Add a local control tunnel and then a LAN-agent apply mode gated behind an
   explicit flag. The first deployed step is only `/api/cudy/agent-preview`.
2. Move provider refresh orchestration behind a Go command wrapper, while
   leaving existing OpenWrt shell scripts as the execution backend.
3. Only after that, consider a minimal offline `/api/agent/config` fallback from
   the synced control-state archive.

Do not move Cudy to main-router duty until both the current Python/OpenWrt path
and the new Go fallback path pass the verification checklist.
