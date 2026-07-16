# Roadmap

Updated: 2026-07-16.

This roadmap contains remaining work only. The verified baseline is recorded
in `docs/current-status.md` and frozen by tag
`snapshot-2026-07-16-android-1.21`.

## Execution Rules

- Keep `uswest` as the primary control-server and Cudy as fallback until a
  separately tested migration says otherwise.
- Change one routing layer at a time: control policy, platform agent, Cudy PBR,
  then main-router ownership.
- Every risky step needs a read-only preview, explicit health gates, an
  independent rollback and a post-change connectivity test.
- Keep the development workstation agent disabled unless its watchdog and
  emergency stop are active.
- Do not combine a provider refresh, agent upgrade and Cudy apply trial in one
  maintenance window.

## Phase 2: Platform Agent Acceptance

### Windows

1. Rebuild the package from the current source and verify its manifest/hash.
2. Run emergency-stop and watchdog tests before enabling the task.
3. Enable the managed task in a controlled window with a direct recovery path.
4. Reboot Windows and verify SSH self-heal, cached-policy fallback, selective
   routes and update reporting.
5. Test Direct, Telegram, ChatGPT, Gemini, Mail.ru and a download/speed target.

Exit criteria: one reboot and a 24-hour run without lost LAN/internet, route
leak, focus-stealing console or manual transport repair.

### Linux

1. Confirm Dima receives Linux `1.20 (21)` through the control update path.
2. Keep normal operation one-click from the UI; no manual Amnezia interface or
   route commands should be required.
3. Test suspend/resume, lid close, reboot and Wi-Fi roaming.
4. Test with Zapret and UFW active, then capture one automatic diagnostic.
5. Verify update, disable, enable and uninstall/restore-direct behavior.

Exit criteria: a 24-48 hour real-world soak with no Wi-Fi loss, boot problem,
DNS breakage or manual recovery.

### Android

1. Run a longer Wi-Fi/background/locked-screen soak on `1.21 (22)`.
2. Verify mobile-data/Wi-Fi transitions and provider reconnects.
3. Resolve or clearly explain the remaining Doze/battery warning.
4. Redesign the main screen around a concise state indicator, current/latest
   version, update action and user routing controls; move diagnostics behind a
   dedicated view.
5. Repeat reboot and route acceptance on at least one additional Android build
   before broad rollout.

Exit criteria: no TUN interruption during policy/probe cycles, automatic reboot
recovery, and a user-facing UI that does not expose raw engine internals.

## Phase 3: Complete Auto And Domain Intelligence

1. Add browser-rendered checks for JavaScript-only geographic decisions while
   preserving existing regex/content checks for ordinary probes.
2. Soak the implemented TTL refresh and bounded 300-domain activity window
   under real traffic. Local regressions cover 305 targets and fresh/stale
   cache behavior; agent-side real-usage reporting remains to be validated.
3. Finish the reviewed daily domain/IP list update flow; unknown traffic stays
   Direct until an admin-approved promotion.
4. Verify global/user default and domain-specific candidate precedence through
   one full production scenario.
5. Keep control-server transport plans minimal so agents start only exits used
   by routes or an active probe window.

Exit criteria: admin sets a global default, a user overrides one service,
Auto selects a content-valid winner, all service dependencies share it, and a
provider failure moves traffic to the next valid candidate.

## Phase 4: Finish Admin And User Lifecycle

1. Audit every current admin tab against the actual data model: users, devices,
   enrollment codes, routes, services, aliases, transports, probes, updates and
   system health.
2. Replace ambiguous actions and dead controls; device enable/disable/delete
   must be reversible and explicit.
   The API lifecycle and immediate token invalidation are regression-tested;
   rendered destructive-action UX remains to be completed.
3. Keep user entry authenticated by agent identity and admin entry protected by
   credentials.
4. Show applied policy, pending probe, current Auto winner, offline agent,
   diagnostics and update status in plain language.
5. Make enrollment and update behavior consistent across Windows, Linux and
   Android.
6. Add rendered UI regression coverage for desktop and mobile widths.

Exit criteria: a new user/device can be enrolled, configured, updated,
disabled, re-enabled and deleted without CLI or database edits.

## Phase 5: Guarded Cudy Apply

Prerequisite: Phases 1 and the relevant Auto checks are green.

1. Capture current PBR, dnsmasq, nft, transport and service state.
2. Run the first uncommitted guarded apply trial without moving DHCP/WAN.
3. Deliberately stop the controlling workstation path and prove the independent
   on-router rollback restores previous files, PBR state and `observe` gate.
4. Inspect counters and Direct/provider routes after rollback.
5. Run a separately committed trial, then return to `observe` and compare the
   resulting state.

Exit criteria: both trials preserve LAN/internet access, critical services and
fallback control, and automatic rollback works without Codex or the workstation.

## Phase 6: Make Cudy The Main Router

Prerequisite: guarded apply is accepted and stable.

1. Keep the captured AirTies configuration as the migration source of truth.
2. Reproduce WAN, LAN, DHCP reservations, DNS, Wi-Fi, port forwards and local
   management access on Cudy.
3. Prepare a physical cable and address rollback that does not depend on VPN or
   the control-server.
4. Add a router-level watchdog for WAN, LAN, control and user-critical services.
5. Move one responsibility at a time, starting with a maintenance window and a
   single test client before changing the whole LAN.
6. Keep AirTies ready for immediate rollback until a multi-day soak passes.

Exit criteria: Cudy survives reboot, provider/control outages and rollback
tests as the LAN gateway without one-to-two-minute traffic stalls.

## Phase 7: Disaster Recovery And Control Migration

1. Verify scheduled SQLite/config backups and Cudy state freshness.
2. Clone `uswest` to a disposable replacement VPS from a current backup.
3. Change the endpoint manifest through Cudy and prove agents discover the new
   primary without individual manual edits.
4. Exercise primary-down operation and return to the restored primary.
5. Document the one-click clone/restore timing and operator checklist.

Exit criteria: a clean replacement server can restore control, UI, policy,
providers and agent updates within one planned maintenance window.

## Phase 8: Native Multi-AWG On Every Agent

Prerequisite: routing policy, recovery, platform lifecycle and Cudy main-router
operation are stable. This is intentionally the final platform phase so a new
transport engine cannot obscure higher-level routing defects.

1. Define one versioned AWG backend contract for create, start, stop, refresh,
   health, counters and cleanup; keep logical server ids and control-server
   policy semantics unchanged.
2. Implement independent native AWG instances for Windows, Linux, Android and
   OpenWrt/Cudy behind each platform's existing transport abstraction.
3. Remove the shared Windows `AmneziaVPN` interface limitation and all reliance
   on a separately installed AmneziaVPN application or UI.
4. Let every agent start only the AWG exits required by active routes or a
   bounded probe window, and stop unused instances without affecting other
   transports.
5. Preserve last-known-good configuration and per-instance recovery across
   agent restart, device reboot and temporary control-server loss.
6. Add safe migration and rollback from existing AWG wrappers/configs on every
   platform, including key and endpoint rotation without exposing secrets.
7. Test concurrent `aktau`, `uswest` and an additional AWG exit while Direct,
   sing-box provider routes, Auto probes and control traffic remain isolated.

Exit criteria: all four agent platforms can run at least three independent AWG
instances concurrently, recover them after reboot/control loss, update one
instance without interrupting the others, and roll back to the previous backend
without losing Direct or control connectivity.
