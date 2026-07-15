# Current Status

Snapshot date: 2026-07-16.

Snapshot tag: `snapshot-2026-07-16-android-1.21`.

This document records the verified live state. Planned work belongs in
`docs/roadmap.md`; historical notes are not operating instructions.

## Repository Baseline

- Branch `main` is clean and pushed to GitHub.
- Android `1.21 (22)` is committed in `4c97412`.
- `secrets/`, APKs, local databases, logs and runtime output remain ignored.
- Current published artifacts:
  - Android `1.21 (22)`, SHA256
    `2846b6385e4c0117e1cfdac050d925f56efa4643bdf46d3497bd556b998fc977`;
  - Linux `1.20 (21)`, SHA256
    `5ac2bf679cdef44e3b6c9db2a7ae0d4fc5703f5efe44aaec3077e452a2f898a2`;
  - Windows `1.19 (20)`, SHA256
    `e2ae82fa701d908339d33e0da8598fcb53c7eb6076dbfd1a2c6856eac7d94738`.

## Primary Control Server

The `uswest` server at `95.182.91.203` is the production control-server.

Verified on 2026-07-16:

- `vpn-control.service` is enabled and active with `Restart=always`;
- `/healthz` and `/readyz` are healthy;
- Auto probe and provider refresh workers are enabled and have no worker error;
- 25 of 31 transports are enabled and no enabled transport is stale;
- 2 of 4 enabled agents are currently online;
- 2 enabled agents are offline or stale;
- 4 failed probe jobs remain inside the one-hour warning window;
- direct SSH audit, restricted tunnel-user deployment and agent update
  downloads work.

The control-server remains authoritative for policy, provider transport plans,
Auto cache, probe jobs, enrollment, agent updates and admin/user UI.

## Cudy Fallback And Router Agent

The Cudy router is currently reachable from the primary LAN at
`192.168.1.174` and from its own LAN as `192.168.8.1`.

Verified fallback state:

- `cudy-fallback` is running;
- the restricted control tunnel is running;
- fallback readiness has zero warnings;
- policy source is live with 22 routes and 5 transports;
- the observer is enabled and continues reporting fresh state.

The Go `cudy-router-agent` remains intentionally in `observe` mode. It does not
own PBR, DHCP or WAN routing.

The strict router-agent gate is currently blocked:

- the ChatGPT critical check through `proxyde` times out;
- `91.105.192.0/23` and `91.108.4.0/22` reference interfaces not present on
  Cudy;
- the preview wants to refresh/restart `proxygb` and prepare/start `proxykz`;
- the guarded preview contains 9 changed files, including transport service
  and sing-box config paths that the first trial deliberately refuses.

No apply trial is allowed until these conditions are reconciled and repeated
strict checks are green.

## Android Agent

Android `1.21 (22)` is published and installed on the physical MIUI phone.

Verified acceptance:

- signed APK and production manifest SHA256 match;
- real reboot delivered `LOCKED_BOOT_COMPLETED` without reading
  credential-encrypted preferences;
- `BOOT_COMPLETED` started the foreground service after the configured network
  delay;
- SSH control, policy fetch and TUN recovered automatically;
- Android reports the VPN network as `VALIDATED`;
- `mail.ru` used `Direct`;
- `chatgpt.com` used `proxyde`;
- Telegram `149.154.160.0/20` used `proxyfr`;
- a production probe job tested `proxyde` and `proxynl` while browser traffic
  stayed active;
- probe jobs now use persistent loopback-only mixed inbounds and do not reload
  or interrupt the active TUN.

Remaining Android concerns:

- the test phone is not in the standard Android Doze whitelist even though
  boot recovery succeeded; a longer locked/background soak is required;
- the current UI is functional but exposes too much technical state and needs
  a clearer user-facing status/version/update design;
- JavaScript-only geographic decisions still require rendered probes.

## Windows Agent

- Windows `1.19 (20)` is published.
- Packaging, cached-policy fallback, emergency stop and watchdog regression
  tests exist.
- The development workstation scheduled task is intentionally disabled.
- Normal traffic on this workstation must not depend on an unaccepted agent
  build while Codex development is active.
- A controlled reboot and connectivity acceptance remains outstanding.

## Linux Agent

- Linux `1.20 (21)` is published.
- The wrapper now explicitly reports transport-management capability.
- The one-click package contains service install, status, diagnostics,
  rollback and bundled sing-box support.
- A long real-world test on Dima's machine is still required for suspend,
  resume, Wi-Fi changes, Zapret, UFW and update behavior.

## Auto, Policy And UI

- Policy precedence is implemented as user domain, user default, global
  domain, global default.
- Ordered candidate lists and `all-rest` are implemented.
- Probe assignment prefers a capable agent that used the domain.
- Global and per-user aliases are isolated and tested.
- Important Service dependency groups can share one cache key, candidate list
  and winner; an isolated production staging test passed and was cleaned up.
- Generic success/failure regexes and known geo-block content checks exist.
- Admin and user UI are operational, but need a focused end-to-end usability
  and lifecycle audit against the current model.

## Non-Negotiable Safety Gates

- Do not enable `cudy-router-agent` apply while strict checks are red.
- Do not move DHCP or WAN ownership from AirTies to Cudy before both guarded
  apply trials pass.
- Do not enable the Windows development task without the independent watchdog
  and tested `Emergency-Stop-Agent.cmd` path.
- Do not treat an HTTP 200 response as service success when content or rendered
  state indicates geographic blocking.
- Keep Cudy fallback data fresh before any `uswest` migration or maintenance.

## Immediate Next Step

Stabilize the Cudy observer preview and recent Auto probe failures without
changing routing. The detailed order and exit criteria are in
`docs/roadmap.md`.
