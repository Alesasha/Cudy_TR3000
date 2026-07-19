# Roadmap

Updated: 2026-07-18.

This roadmap contains remaining work only. The verified baseline is recorded
in `docs/current-status.md` and frozen by tag
`snapshot-2026-07-18-android-code-enrollment-1.25`. Current work builds on that
accepted baseline.

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
3. Enable the managed task in a controlled window with the independent recovery path.
4. Reboot Windows and verify SSH self-heal, cached-policy fallback, selective
   routes and update reporting.
5. Test Direct, Telegram, ChatGPT, Gemini, Mail.ru and a download/speed target.

Exit criteria: one reboot and a 24-hour run without lost LAN/internet, route
leak, focus-stealing console or manual transport repair.

### Linux

1. Confirm Dima receives Linux `1.23 (24)` through the control update path and
   reports the expected non-empty TUN interface set after OFF/ON.
2. Keep normal operation one-click from the UI; no manual Amnezia interface or
   route commands should be required.
3. Test suspend/resume, lid close, reboot and Wi-Fi roaming.
4. Test with Zapret and UFW active, then capture one automatic diagnostic.
5. Verify update, disable, enable and uninstall/restore-direct behavior.

Exit criteria: a 24-48 hour real-world soak with no Wi-Fi loss, boot problem,
DNS breakage or manual recovery.

### Android

The production agent now keeps recent compatible probe transports warm for six
hours. A live cycle accepted a new probe job without changing the unified
config hash or reloading libbox; the longer soak below remains required.

1. Continue the multi-day Wi-Fi/mobile-data/background/locked-screen soak on
   the two physical phones with `1.29 (30)`. Run `android-agent-soak.ps1` during
   focused sessions so a failure retains service, job, package, VPN, and crash
   evidence.
2. Verify mobile-data/Wi-Fi transitions and provider reconnects.
3. Soak the implemented MIUI Autostart confirmation. The app now distinguishes
   standard permissions it can verify from the vendor-only setting that Android
   cannot query, and stops repeating the warning after explicit confirmation.
4. Soak the 1.29 state-aware first screen and recovery paths: diagnostics,
   routing details and advanced settings are collapsed until requested; sticky
   restarts reload persisted settings; the delayed recovery job must restart an
   unexpectedly killed requested agent without overriding a manual Stop. This
   acceptance passed on the Xiaomi Mi Note 10 Lite; keep it in the longer soak.
5. Repeat reboot and route acceptance on at least one additional Android build
   before broad rollout.

Exit criteria: no TUN interruption during policy/probe cycles, automatic reboot
recovery, and a user-facing UI that does not expose raw engine internals.

## Phase 3: Complete Auto And Domain Intelligence

1. Add browser-rendered checks for JavaScript-only geographic decisions while
   preserving existing regex/content checks for ordinary probes.
   Technical dependency hosts now inherit a canonical user-facing probe URL
   from their service group: for example, `googlevideo.com` is tested through
   YouTube and OpenAI static hosts are tested through ChatGPT. Production
   verified the YouTube path on an Android agent; rendered geo checks remain.
2. Soak the implemented TTL refresh and bounded 300-domain activity window
   under real traffic. Local regressions cover 305 targets and fresh/stale
   cache behavior; agent-side real-usage reporting remains to be validated.
3. Finish the reviewed daily domain/IP list update flow. Provider refresh is
   already automatic, but the managed-domain source is not; the Reuters miss
   demonstrated this gap. Unknown traffic stays Direct until an admin-approved
   promotion.
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
   destructive user/device actions now have explicit wording, confirmations
   and a production desktop/mobile render check.
3. Keep user entry authenticated by agent identity and admin entry protected by
   credentials.
4. Show applied policy, pending probe, current Auto winner, offline agent,
   diagnostics and update status in plain language.
5. Make enrollment and update behavior consistent across Windows, Linux and
   Android.
6. Extend the implemented APK download and protected Android code-only
   enrollment flow to equivalent one-click Windows/Linux installers. Android
   uses a shared SSH bootstrap account restricted to the enrollment-only port,
   then receives a unique per-device SSH key and token after consuming the code.
   Universal Windows/Linux packages and enrollment clients are now built with
   pinned SSH host identity and no personalized device material. The Devices
   workflow serves the correct bootstrap package for Android, Windows or Linux.
   One fresh-device acceptance per desktop platform is still required.
   Android `1.27` also has the minimal in-app credential-protected admin surface;
   extend it only after the current user/device/enrollment workflow is soaked.
7. Add automated rendered UI regression coverage for desktop and mobile
   widths; the current production UI has passed the equivalent manual check.
   Users/Agents filtering and stale default-server option reduction are now
   covered by the HTTP smoke test and manual browser interaction; broader
   rendered automation remains pending.

Exit criteria: a new user/device can be enrolled, configured, updated,
disabled, re-enabled and deleted without CLI or database edits.

## Phase 5: Guarded Cudy Apply

Prerequisite: Phases 1 and the relevant Auto checks are green.

1. Keep the router-agent in `observe` and eliminate fallback-preview timeouts
   and critical-service probe flaps; require repeated strict checks to pass.
2. Deploy the LokVPN Reality `short_id` stabilization separately, allow one
   natural provider refresh, and require zero recurring transport actions.
   This is complete for short-ID-only churn. Real endpoint identity changes
   remain legitimate guarded transport refreshes.
3. Capture current PBR, dnsmasq, nft, transport and service state.
4. Completed: the corrected uncommitted route trial applied without moving
   DHCP/WAN.
5. Completed: its independent on-router timer restored previous files, 24 PBR
   rules, `observe` and the closed apply gate without workstation intervention.
6. Completed: post-rollback strict fallback/observer checks and Direct/provider
   state were healthy.
7. Completed: a separately committed route trial retained generated route files,
   returned the agent to `observe`, and passed strict 5/5 critical checks with
   zero blockers, warnings, changed files or transport actions.

Exit criteria met on 2026-07-18. Continue an observe/traffic soak before Phase 6
changes DHCP, WAN or physical cabling.

Direct SSH to `uswest` must also complete reliably before this phase. An open
TCP/22 socket alone is not sufficient; the SSH banner/session, restricted Cudy
tunnel and fallback control path must be verified independently.

## Phase 6: Make Cudy The Main Router

Prerequisite: guarded apply is accepted and stable.

1. Keep the captured AirTies configuration as the migration source of truth.
   A fresh redacted Cudy snapshot and offline preflight now report zero hard
   failures; the snapshot collector is read-only and records command status.
2. Reproduce WAN, LAN, DHCP reservations, DNS, Wi-Fi, port forwards and local
   management access on Cudy. Current blockers-to-review are ISP VLAN 2,
   disabled/unencrypted Cudy Wi-Fi, six forward targets without AirTies DHCP
   reservations, and four maintenance host routes through the old gateway.
3. Prepare a physical cable and address rollback that does not depend on VPN or
   the control-server. The independent configuration rollback service is now
   installed, disarmed and backed up; physical rollback is still required.
4. Completed for cutover safety: install an independent structural
   LAN/default-route/WAN-gateway guard with timed rollback. Add non-destructive
   user-critical service monitoring separately after Cudy owns the LAN.
5. Move one responsibility at a time, starting with a maintenance window and a
   single test client before changing the whole LAN.
6. Keep AirTies ready for immediate rollback until a multi-day soak passes.

Exit criteria: Cudy survives reboot, provider/control outages and rollback
tests as the LAN gateway without one-to-two-minute traffic stalls.

## Phase 7: Disaster Recovery And Control Migration

1. Completed on 2026-07-18: the scheduled backup uses the private Cudy path,
   has a zero last result, and the newest archive passed required-member,
   metadata, secret-presence and SQLite integrity checks. Cudy fallback state
   remains independently freshness-checked.
2. Clone `uswest` to a disposable replacement VPS from a current backup.
   The local isolated restore rehearsal passed on 2026-07-18, including safe
   extraction, restored DB summary, `/healthz` and `/readyz`; the clean-VPS
   network/systemd test remains.
3. In progress: Android, Linux and Windows cache a primary endpoint and SSH
   fingerprint received through authenticated agent policy, preserving each
   device's SSH user/key. Build and publish the updated agents, then prove a
   controlled endpoint rotation without individual manual edits. A signed
   public discovery contract remains for unplanned loss before policy delivery.
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
