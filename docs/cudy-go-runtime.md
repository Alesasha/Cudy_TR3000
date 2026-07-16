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
GET /api/cudy/agent-observer
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

The background observer refreshes the policy every minute and writes the last
known good control response atomically to:

```text
/var/lib/cudy-fallback/agent-config-cache.json
```

The cache directory is mode `0700` and the cache file is mode `0600`. If the
primary control connection is temporarily unavailable, `agent-preview` accepts
the cache for up to 24 hours and reports `source=cache`, its age, and the live
control error. `/api/cudy/agent-observer` reports the last attempt, last
success, cache update, and current error. This is observation/fallback state;
it still does not apply routes.

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
  --identity-file secrets\cudy-control-tunnel\control_tunnel_dropbear_ed25519 `
  --control-user cudy-tunnel-cudy `
  --enable --start
```

Use a private key generated by the router's current Dropbear build. Dropbear
2025.89 on the deployed router rejects an OpenSSH Ed25519 private-key file even
though the public key algorithm itself is supported. The installer writes
`tunnel.env` with explicit LF line endings; CRLF leaves carriage returns in
OpenWrt shell variables and prevents `procd` from starting the tunnel.

This tunnel still does not apply routes; it only makes the control API
reachable for the observer and preview endpoints.

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

1. Keep the deployed observer in read-only mode while a separate
   `cudy-router-agent` implements explicit `disabled`, `observe`, and `apply`
   modes with an atomic rollback journal.
2. Move provider refresh orchestration behind a Go command wrapper, while
   leaving existing OpenWrt shell scripts as the execution backend.
3. Only after that, consider a minimal offline `/api/agent/config` fallback from
   the synced control-state archive.

Do not move Cudy to main-router duty until both the current Python/OpenWrt path
and the new Go fallback path pass the verification checklist.

## PBR Safety Layer

PBR must not use its package-provided early boot entry on this router. Provider
tunnels appear asynchronously and their interface events can request another
PBR rebuild immediately after the first one. A rebuild also temporarily changes
forwarding state, so a failed or repeated rebuild can disconnect the LAN.

The production boot path is therefore:

```text
cudy-pbr-watchdog (START=97, recover once then fail open)
cudy-pbr-safe      (START=99, delayed serialized start)
```

Install it first without starting PBR:

```powershell
python tools\deploy_cudy_pbr_safety.py
```

Run a controlled rebuild only while an independent management path is active:

```powershell
python tools\deploy_cudy_pbr_safety.py --start-pbr
```

The deploy helper disables the default `pbr` rc.d boot link, sets
`strict_enforcement=0`, disables nft set auto-merge, coalesces
interface-triggered reloads, enables the watchdog, and leaves IPv4 forwarding
enabled. The bundled `cudy-cidr-collapse` tool normalizes downloaded and
override IPv4 ranges before they enter an interval set; this avoids both
overlap errors and the nftables auto-merge serialization crash. Any validation
failure stops PBR and preserves direct WAN forwarding. The watchdog first
attempts one serialized safe restart when the real nft/ip-rule dataplane
disappears; if recovery fails, it stops PBR and preserves direct WAN instead
of entering a restart loop.

## Router Agent

`cmd/cudy-router-agent` is deployed as a separate OpenWrt service and failure
domain. Its production init script currently hard-codes `-mode observe`.

```powershell
powershell -ExecutionPolicy Bypass -File tools\Build-CudyRouterAgentGo.ps1
python tools\deploy_cudy_router_agent.py
python tools\check_cudy_router_agent.py --strict
```

Observe mode:

- reads the sanitized preview and the root-only full LKG cache;
- renders and validates missing sing-box transports with `sing-box check` in a
  temporary mode-0600 directory;
- calculates PBR override changes while preserving every manual line outside
  the `cudy-router-agent` marker block;
- validates `http-proxy-tun` exits through their upstream HTTP proxy from the
  root-only cached transport plan; AWG/VLESS checks continue to use the
  interface and OpenWrt PBR fwmark fallback;
- writes only `/var/lib/cudy-router-agent/{desired,diff,status}.json`;
- never creates a transport service, restarts PBR, or changes a route.

Apply mode exists in the binary but is not enabled by the deployed init script.
It requires both `-mode apply` and `-allow-apply`. It backs up every affected
file under a timestamped root-only transaction directory, starts only required
transports, updates PBR atomically, and restores the previous files and PBR
state if transport startup, PBR restart, or the post-apply health request fails.

The first live apply must use the independent guarded trial tool. Its default
invocation is read-only and prints the exact eligible files:

```powershell
python tools\trial_cudy_router_agent_apply.py
```

The tool refuses stale or unhealthy observer state, blockers, unsafe paths and
plans that need a new transport. `--apply --yes` arms a rollback process on the
router before changing the persistent mode gate. If the workstation or SSH
session disappears, Cudy restores the previous override files, runs the
fail-open PBR restart and returns the agent to `observe`. A successful result is
retained only with the additional `--commit` gate.

The current observe plan contains 22 routes, eight PBR override changes and no
dynamic transport action. Critical health is 5/5 and three strict checks passed
at least five minutes apart. Keep observe mode active until the independent
rollback guard has been exercised in the first controlled apply trial.
