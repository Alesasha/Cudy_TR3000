# Current Status

Verified on 2026-07-15 from the development workstation and live Cudy router.

## Working

- The workstation runs in Cudy-only mode: Ethernet via `192.168.8.1`, with the
  standard AmneziaVPN application and Wi-Fi fallback disabled.
- Cudy selective routing reaches `uswest`; Telegram TCP, Gemini and ChatGPT
  transport checks succeed.
- The delayed serialized PBR boot path, fail-open watchdog, forwarding and
  firewall validation are deployed.
- `cudy-fallback` and the restricted Cudy control tunnel are running. A strict
  check observed live policy with 22 routes and 7 transports.
- `cudy-router-agent` is running in `observe`. It receives live policy, reports
  agent heartbeat, processes Auto probe jobs and passes its strict check with
  22 routes and five healthy critical services. It still does not modify PBR.
- Provider refresh cron entries exist for VPNtype and LokVPN.
- Content-aware Auto probes now run on the requesting agent. The Python and Go
  implementations reject HTTP responses containing known geographic-block
  text, including HTML-escaped apostrophes. JavaScript-rendered geo decisions
  remain a separate probe limitation.
- Primary control loss and recovery were exercised: Cudy served cached policy
  while its control tunnel was stopped, then returned to live policy after the
  tunnel restarted without changing the WAN/default route.
- The production `uswest` audit passes over direct SSH. Ten consecutive login
  and command sessions completed, `sshd` reported no `MaxStartups` drops or
  restarts, and the control service, readiness checks and background workers
  were healthy.
- The SSH watchdog, firewall guard and `fail2ban` are active. Public scans are
  being rejected without restricting roaming agents to fixed source IPs.
- Control backup and Cudy fallback sync tasks are installed on Windows.
- Targeted control, Auto, packaging, watchdog, PBR and Go regression tests pass.
- No file under `secrets/` is tracked by Git.
- Android `1.20 (21)` is published on the production control-server and running
  on the physical phone with
  full IPv4 TUN capture, TUN DNS, protected direct/provider sockets and SNI
  routing. Android reports the VPN as `VALIDATED`; `example.com` used `Direct`,
  `chatgpt.com` used `proxynl`, and Telegram `149.154.160.0/20` used `proxynl`.
  Repeated unchanged policy cycles did not reload libbox.
- The first admin/UI audit is complete. The admin page now uses focused tabs.
  Global lookup aliases remain admin-owned, while every user can create local
  aliases that override the same global name only for that account. HTTP and
  rendered mobile UI checks cover precedence, isolation, deletion fallback and
  shared-dictionary authorization.
- Auto probe assignment now checks transport-management capability both when a
  job is scheduled and when an agent claims it. The Cudy observer no longer
  advertises transport management while in `observe`; stale assignments were
  reconciled and no active probe job remains assigned to `cudy-home`.
- Important Services can now optionally act as dependency groups. Every target
  hostname in one enabled group shares a single Auto cache key, candidate list
  and winner. Global and per-user groups are isolated, explicit user-domain
  routes stay highest priority, and all existing services migrate as
  `health only` until routing is enabled explicitly.
- The alias and dependency-group build is deployed on `uswest`. A production
  staging user proved that two dependent domains received one cache key, one
  `proxyde` winner and the expected transport plan; all staging rows were then
  removed. The strict production audit remains green.
- A guarded Cudy apply-trial tool now validates observer freshness, critical
  services, paths and transport actions before it can arm apply. Its rollback
  process runs on Cudy and restores the previous overrides, managed-path state,
  PBR state and persistent `observe` gate if the workstation disappears. The
  live preflight is green for seven existing PBR override files; apply has not
  been enabled.
- Linux now reports transport-management capability explicitly from its
  managed wrapper instead of inheriting the old Windows-only platform default.
  This allows the capability-gated Auto worker to assign provider probe jobs to
  a Linux agent that can actually start its `transport_plan`.

## Incomplete Or Degraded

- Historical SSH banner failures remain a condition to monitor, but they are
  not currently reproducible. The production audit needed a 60-second timeout
  because its complete remote status check can legitimately exceed 30 seconds.
- `cudy-router-agent` apply remains intentionally disabled. Strict preflight is
  now green, but the current preview still contains seven PBR file changes and
  needs a controlled reversible acceptance window before any apply trial.
- The Windows managed-agent task is disabled on the development workstation;
  current traffic is intentionally handled by Cudy instead.
- A real locked-boot test found that Android `1.20 (21)` touches
  credential-encrypted preferences too early on MIUI. Android `1.21 (22)` moves
  the locked-boot marker to device-protected storage and waits for user unlock
  before reading enrollment secrets; the signed build exists locally but is
  intentionally not published until it passes install, locked reboot and
  post-unlock acceptance on the physical phone. The signed-in Chrome profile
  still reports Gemini
  `location_rejected` even when a temporary Android-only experiment routed the
  full Google dependency set through one French exit; those broad temporary
  rules were removed. The dependency-group routing foundation is complete, but
  browser-rendered probes for JavaScript-only geo decisions remain open work.
- The Linux agent needs a longer real-world soak test covering suspend/resume,
  Wi-Fi changes, Zapret and UFW.
- Production currently reports one of four enabled agents online. Historical
  OpenAI probe failures remain visible for the one-hour warning window, but
  Cudy no longer claims jobs that require transports it cannot start in
  observer mode. Two old unassigned Speedtest jobs remain pending for a capable
  agent.

## Next Order Of Work

1. Unlock the physical phone, install Android `1.21 (22)`, and pass locked
   reboot plus post-unlock recovery before publishing it.
2. Complete Windows and Linux production acceptance separately.
3. Add browser-rendered probes for JavaScript-only geo decisions.
4. Run an uncommitted guarded Cudy apply trial and verify its automatic
   rollback without moving DHCP/WAN; only then run a separately committed trial.
5. Prepare a staged, reversible migration from AirTies to Cudy as main router.

Do not enable router-agent apply mode or move DHCP/WAN ownership to Cudy until
fallback recovery and critical-service transport checks have passed.
