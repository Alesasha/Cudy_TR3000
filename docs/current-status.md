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
  - Windows `1.20 (21)`, SHA256
    `8dba7836dbd9172445e7df8af2647116cddc62bc4bd1cbb0588ba5dad8f1b6d8`.

## Primary Control Server

The `uswest` server at `95.182.91.203` is the production control-server.

Verified on 2026-07-16:

- `vpn-control.service` is enabled and active with `Restart=always`;
- `/healthz` and `/readyz` are healthy;
- Auto probe and provider refresh workers are enabled and have no worker error;
- 25 of 31 transports are enabled and no enabled transport is stale;
- 2 of 4 enabled agents are currently online: Cudy and Android;
- Windows is intentionally disabled on the development workstation and the
  Linux agent is offline/stale pending the next real-world acceptance run;
- operational probe warnings are zero; nine recent failed apex probes are
  classified as suffix targets without DNS records and do not trigger retries
  for 24 hours or degrade readiness;
- direct SSH audit, restricted tunnel-user deployment and agent update
  downloads work.
- the scheduled operator backup and Cudy fallback-sync tasks both have a zero
  last result; the latest pulled backup archive is dated 2026-07-16.

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

The read-only router-agent gate is green:

- critical health is 5/5, including ChatGPT through `proxyde`;
- blockers, warnings and transport actions are zero;
- 22 effective routes produce eight PBR override file changes only;
- `check_cudy_router_agent.py --expected-mode observe --strict` passed three
  times at least five minutes apart;
- guarded apply preflight passes and still requires explicit `--apply --yes`.

The previous ChatGPT timeout was a router-local TUN diagnostic artifact, not a
provider outage. For `http-proxy-tun` transports the observer now probes the
upstream HTTP proxy from the root-only cached transport plan. User traffic and
live PBR remain unchanged. The agent is still in `observe` mode.

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
  or interrupt the active TUN;
- a forced Wi-Fi outage kept the foreground service alive; after Wi-Fi returned
  the agent recreated the TUN and Android reported the VPN `VALIDATED` again.

Remaining Android concerns:

- the test phone is not in the standard Android Doze whitelist even though
  boot recovery succeeded; a longer locked/background soak is required;
- the current UI is functional but exposes too much technical state and needs
  a clearer user-facing status/version/update design;
- JavaScript-only geographic decisions still require rendered probes.

## Windows Agent

- Windows `1.20 (21)` is published and its production manifest/hash match.
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
- Default apex probes that every candidate reports as `resolve_failed` use a
  bounded 24-hour negative cache. This supports suffix routes such as
  `oaiusercontent.com` whose apex intentionally has no address while keeping a
  periodic retry.
- The background scheduler now considers at most 300 recently changed or
  promoted Auto targets per cycle. Fresh winners are skipped until their TTL
  expires; stale winners become probe-eligible again. Worker status reports the
  selected window and total target counts. Regression tests cover a 305-domain
  set and fresh/stale cache behavior; a production traffic soak is still needed
  before calling the real-usage requirement complete.
- Global and per-user aliases are isolated and tested.
- Important Service dependency groups can share one cache key, candidate list
  and winner; an isolated production staging test passed and was cleaned up.
- Generic success/failure regexes and known geo-block content checks exist.
- Recent Auto history now returns failed jobs with per-candidate reasons,
  latency and HTTP status; the admin candidate editor displays up to three
  recent failures next to the last ten winners.
- Agent-token user entry, credential-protected admin entry, Route Lookup,
  Auto cache, agent controls and failure history were rendered and exercised
  against production on desktop and a 375-pixel mobile viewport.
- Admin and user pages have no document-level horizontal overflow or browser
  console errors; wide admin tables scroll inside their section.
- Control JSON uses conditional gzip and the admin page loads its data and
  system status concurrently. The external private-Cudy status probe is cached
  for 60 seconds instead of adding a three-second timeout to every refresh.
- The status page distinguishes active/enabled transports, reports provider
  refresh counts, and labels the private Cudy check as unreachable from the VPS
  instead of incorrectly calling it stale. The independent Cudy checks remain
  authoritative for fallback health.
- Full enrollment/update/device lifecycle usability still needs the remaining
  Phase 4 audit and automated rendered regression coverage.
- The HTTP lifecycle regression now creates and deletes a user, revokes and
  consumes one-time enrollment codes, rejects code reuse, and verifies that
  disabled/deleted device tokens fail immediately while Enable restores the
  same token. Agent token caches are invalidated on every device state change.
  Destructive actions now use explicit labels and confirmations: user deletion
  distinguishes account-only removal from legacy Cudy peer revocation, and
  device controls say `Apply state` / `Delete device`. These controls were
  rendered against production on desktop and at 375 pixels without page-level
  overflow or console errors. Automated rendered regression coverage remains.

## Non-Negotiable Safety Gates

- Do not enable `cudy-router-agent` apply outside the guarded trial with its
  independent timed rollback, even though observe checks are green.
- Do not move DHCP or WAN ownership from AirTies to Cudy before both guarded
  apply trials pass.
- Do not enable the Windows development task without the independent watchdog
  and tested `Emergency-Stop-Agent.cmd` path.
- Do not treat an HTTP 200 response as service success when content or rendered
  state indicates geographic blocking.
- Keep Cudy fallback data fresh before any `uswest` migration or maintenance.

## Immediate Next Step

Complete platform-agent acceptance, starting with a controlled Windows run
behind the independent watchdog while Android continues its background soak.
Linux acceptance remains dependent on Dima's next real-world session. The
detailed order and exit criteria are in `docs/roadmap.md`.
