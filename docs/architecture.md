# Architecture

## Goal

The project provides centrally managed selective routing for individual devices
and whole LANs. An administrator defines global policy, users may add local
overrides, and each agent applies the resulting policy as close to the client as
its platform allows.

The design keeps the control plane separate from the traffic path. Losing the
primary control-server must not immediately break already applied routing.

## Topology

```text
                         desired state and updates
                 +------------------------------------+
                 | uswest primary control-server      |
                 | Python app, SQLite, workers, UI    |
                 +------------------+-----------------+
                                    |
                         per-device authenticated API
                                    |
              +---------------------+---------------------+
              |                     |                     |
       Windows/Linux agent    Android VpnService     Cudy/OpenWrt
       local routes + exits   local TUN + exits      LAN PBR + exits
              |                     |                     |
              +---------------------+---------------------+
                                    |
                 own AWG, VPNtype and LokVPN exits

 Cudy also keeps a restricted tunnel, endpoint manifest, control-state backup,
 and compact Go fallback service for primary discovery and recovery.
```

## Control Plane

The primary control-server on `uswest` owns desired state:

- users and devices;
- global and per-user domain and IP/CIDR rules;
- ordered Auto candidate policies and winner cache;
- provider transport plans;
- probe jobs and results;
- agent health, diagnostics, enrollment and update manifests;
- administrator and user web interfaces.

Agents fetch policy through the authenticated agent API and post status and
probe results. Provider credentials remain on the control-server; agents
normally receive ready transport configurations rather than calling provider
APIs themselves.

SQLite is sufficient for the current scale. Backups and endpoint manifests are
periodically copied to Cudy so a replacement VPS can be bootstrapped without
recreating users and policy manually.

## Effective Policy

Auto candidate lists are resolved in this order:

1. user domain override;
2. user default;
3. global domain policy;
4. global default.

A route target can be:

- `direct`, which explicitly bypasses managed exits;
- a concrete server id;
- `auto`, which resolves through the current winner cache and ordered candidate
  policy.

Domains and IP ranges present in the maintained tunnelling lists are managed as
Auto unless a more specific rule selects `direct` or a concrete server. Targets
outside all maintained and explicit rules remain direct and may be recorded in
the domain-discovery review queue.

The Auto worker prefers a capable agent that recently used the target. If no
such agent is online, the control-server performs the probe. Candidate windows
are bounded so agents do not start every provider transport at once. Success
semantics may include HTTP status, latency, throughput and content validation;
a fast HTTP 200 containing a geographic-block page is a failure, not a winner.

## Transport Layer

Logical server ids are stable policy names. Runtime interface names are an
implementation detail.

- Own exits: `aktau` and `uswest`, currently AmneziaWG interfaces on Cudy.
- VPNtype: sing-box TUN transports such as `proxyde`, `proxynl` and `proxyus`.
- LokVPN: logical profiles such as `lokvpn-de1`; runtime transports are created
  only while a route or probe needs them and should be removed when unused.

The current Windows AWG wrapper exposes one shared `AmneziaVPN` interface, so it
cannot use `aktau` and `uswest` concurrently. A native multi-interface AWG
backend is deliberately deferred until the rest of the system is stable.

## Platform Agents

### Windows

The managed agent applies routes in one bounded PowerShell batch, starts only
required transports, caches the last known good policy and maintains the
control tunnel. An independent watchdog can restore direct routing and stop all
managed components when critical connectivity repeatedly fails.

### Linux

The Linux package installs a systemd service, sing-box runtime, desktop UI,
diagnostics, update helpers and an independent watchdog. It must coexist with
NetworkManager, systemd-resolved, UFW and optional Zapret without requiring
manual routes.

### Android

The Android application owns an Android `VpnService` and libbox engine. It can
enroll with a one-time code, fetch policy, start provider transports, run probe
jobs and report status. Explicit IP routes work. Full domain/SNI equivalence
still requires a protected loop-free direct outbound before the app can safely
capture a default route.

### Cudy/OpenWrt

Cudy has two independent roles:

1. LAN-wide data-plane agent using PBR and provider interfaces;
2. emergency fallback control path.

The Go `cudy-fallback` process is read-mostly and serves endpoint/state status
and cached policy preview. The separate Go `cudy-router-agent` renders desired
OpenWrt artifacts. Its rollout states are `disabled`, `observe` and `apply`;
apply is not enabled until repeated observe diffs and connectivity gates pass.

PBR uses a delayed serialized boot wrapper and a fail-open watchdog. Validation
failure stops PBR while keeping IPv4 forwarding and direct WAN available.

## Failure Behaviour

- Primary API unavailable: agents use cached policy; Cudy publishes fallback
  endpoint metadata.
- Primary VPS moved: update the manifest on Cudy, then agents discover the new
  endpoint.
- Provider exit unavailable: Auto selects the next healthy candidate.
- Agent apply failure: platform watchdog restores a known direct baseline.
- PBR rebuild failure: Cudy fails open instead of disabling LAN forwarding.
- Cudy unavailable: device agents continue routing independently.

## Source Of Truth

- `tools/vpn_control_app.py`: current control-server and UI behaviour.
- `tools/route_agent.py`: shared desktop policy planner/applicator.
- `apps/CudyAndroidAgent/`: Android implementation.
- `cmd/cudy-fallback/` and `cmd/cudy-router-agent/`: Go OpenWrt services.
- `openwrt/`: deployed router scripts and init files.
- `config/vpn_inventory.json`: static server catalog.
- `docs/verification.md`: verified capabilities and remaining work.
- `docs/current-status.md`: latest dated operational snapshot.

Historical `MAIN.md` and `BRANCH-*.md` files are context only and must not be
used as current operating instructions.
