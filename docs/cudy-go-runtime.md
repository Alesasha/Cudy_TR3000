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

1. Add a local agent loop that pulls `/api/agent/config` from primary control
   and applies only LAN-wide Cudy routes.
2. Move provider refresh orchestration behind a Go command wrapper, while
   leaving existing OpenWrt shell scripts as the execution backend.
3. Only after that, consider a minimal offline `/api/agent/config` fallback from
   the synced control-state archive.

Do not move Cudy to main-router duty until both the current Python/OpenWrt path
and the new Go fallback path pass the verification checklist.
