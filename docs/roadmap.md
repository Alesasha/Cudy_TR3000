# Roadmap

This is the current implementation order for the managed VPN control project.

## 1. Repository Hygiene

- Keep `.gitignore` strict for `secrets/`, `backups/`, build outputs, APK/AAB,
  local DBs, logs, and runtime artifacts.
- Keep `apps/`, `tools/`, `deploy/`, `openwrt/`, and `docs/` separated by
  responsibility.
- Keep `README.md` focused on bootstrap commands and link deeper docs.
- Commit every stable working point.

## 2. Control-Server Production

- Keep `uswest` as primary control-server.
- Verify `vpn-control.service`: autostart, restart policy, logs, and health.
- Run provider refresh, Auto worker, stale probe cleanup, and backup as regular
  jobs.
- Maintain SQLite/config backups.
- Add a clear health/status page with provider status, agents, last policy,
  last probes, and backup age. Machine-readable endpoints are `/api/status`
  for admins and public `/readyz` for production readiness checks.

## 3. Fallback Control Path

- Keep Cudy as an emergency fallback control path.
- Sync a lightweight endpoint manifest from primary to Cudy.
- Sync a secret full control-state archive from primary to Cudy under `/root`,
  with only a non-secret freshness status under `/www`.
- Backup/fallback archive publishing has local regression coverage for pruning,
  archive upload metadata, public `state.json`, public `endpoints.json`, and
  current-state symlink behavior.
- Agents should know a primary URL, optional direct fallback URLs, and static
  endpoint manifest URLs.
- If `uswest` is rebuilt or moved, update the Cudy manifest first; agents can
  discover the new primary through Cudy.
- Windows agent can already read the Cudy manifest and switch the SSH tunnel
  host for the next control connection.
- Later, replace the static manifest with a compact Go fallback service on Cudy
  that can serve minimal policy, not only endpoint discovery.
- The first Go fallback service now lives in `cmd/cudy-fallback`. It keeps the
  current static artifact contract and serves `/healthz`, `/readyz`,
  `/api/control/endpoints`, `/cudy-control/endpoints.json`, and
  `/cudy-control/state.json`.

## 4. Auto Mode

- Remove the separate `Auto Candidate Lists` UI concept.
- In Global Domain Routes and per-user Domain Routes, use one ordered candidate
  list format, for example `proxyde, proxynl, all-rest`.
- Resolution priority:
  1. user domain;
  2. user default;
  3. global domain;
  4. global default.
- Auto worker should first assign probe jobs to agents that actually used the
  domain. If no live agent is available, test from the control-server.
- Keep winner history and a TTL cache.
- Show recent winners with latency and throughput near candidate-list editing.
- Current implementation expands `all-rest` on the control-server and sends
  bounded probe windows to agents (`8` candidates by default), so agents do not
  start every provider transport at once.
- Regression coverage now verifies that agent `transport_plan` contains only
  transports needed by applied routes and pending probe jobs.
- Regression coverage now verifies that Auto probe jobs prefer an active agent
  that already reported the target domain.
- Provider-transport probe jobs now require an agent that reports
  `can_manage_transports=true`; this prevents the scheduler from assigning
  LokVPN/VPNtype probe windows to a client that can route but cannot start those
  exits.

## 5. Provider Transports

- Control-server is the normal source of fresh LokVPN/VPNtype transport data.
- Agents receive ready `transport_plan` entries and should not regularly call
  provider APIs.
- Agent-side provider refresh remains only as fallback.
- Start only needed LokVPN/VPNtype transports; stop unused slots immediately.
- LokVPN profiles missing from the current subscription are marked as disabled
  transport configs, so they stop participating in Auto until the subscription
  returns them again.

## 6. Windows Agent

- Production install/uninstall is implemented through scheduled task helpers.
- Emergency rollback is implemented as `Emergency-Stop-Agent.cmd/.ps1`: it
  stops and disables the scheduled task, kills managed child processes,
  stops SSH/sing-box/AWG transports, removes their routes, and restores direct
  IPv4 routing and DNS.
- SSH tunnel self-heal is implemented through endpoint manifest/fallback logic.
- Sing-box transport self-heal is implemented for control-server
  `transport_plan`; unused exits are stopped automatically.
- Route application uses one bounded PowerShell batch instead of one process
  per route. A live production cycle on 2026-07-13 applied 74 commands without
  losing connectivity.
- Fresh policy is fetched once per cycle and the exact cached snapshot is then
  applied. The same cache is the offline fallback input.
- Cached fallback was exercised on 2026-07-13 with control deliberately marked
  offline: 73 route commands were applied, status/probes were skipped, and
  HTTPS connectivity remained available.
- An independent Windows safety watchdog runs outside the agent. The agent
  writes a heartbeat only after a successful route apply; the watchdog also
  checks general internet and a local cached list of user-critical services.
  Repeated failures are reported to the control-server and trigger the full
  emergency direct-route restore. The Codex API check is configured only on
  the development workstation, not as a production default for every user.
- Control-server transports override legacy task arguments. Reinstall the
  existing task with `-NoDirectTransports` to remove the obsolete argument
  from Task Scheduler as well.
- Temporary backend limitation: the current AmneziaWG service wrapper exposes
  one `AmneziaVPN` interface. If `aktau` and `uswest` are both requested, the
  agent selects the more-used exit for that shared interface and reports the
  aliasing explicitly. Independent simultaneous own-server exits require the
  deferred `cudy-awg-native` backend.
- Clear logs and status command.
- Smoke-test after Windows reboot.
- Managed routing smoke now accepts any active managed exit for Auto-routed
  targets, for example `proxyde` for `ifconfig.me` and `proxynl` for Telegram
  CIDRs.
- Verify Telegram, Gemini, ifconfig, speedtest, and direct traffic.

## 7. Android Agent

- Normal user instruction exists in `docs/android-agent.md`.
- Real reboot on the MIUI test phone was verified after enabling Autostart and
  unrestricted battery mode.
- Battery/VPN/MIUI Autostart readiness is shown on the app main screen.
- First-run setup now requests notification permission, Android VPN permission,
  battery optimization exemption, and opens MIUI Autostart/app settings.
- Battery restrictions and foreground service behavior are documented.
- Production probe-job support is implemented through Android local mixed proxy
  probes. HTTP(S) jobs use the proxy directly; `tcp://host:port` jobs use HTTP
  `CONNECT`, which is verified against Telegram IP ranges through each exit.
- Boot/reconnect receiver is implemented and verified through both explicit
  test broadcast and a real reboot on the current test phone.
- Real reboot smoke now verifies `BOOT_COMPLETED -> foreground service ->
  policy fetch -> engine=running`; the boot path waits briefly for Android
  networking before the first SSH control fetch.
- Release `1.19 (20)` is built, published through the control-server update
  manifest, and installed on the physical MIUI phone. One-time enrollment was
  reissued without clearing app data; policy fetch, SSH control, status post,
  foreground service, libbox engine, and selective `tun0` routing all passed.
- The libbox platform now publishes actual Android interface inventory and
  keeps one default-network callback across repeated config reloads. A live
  multi-cycle check stayed at one callback with zero interface lookup errors.
- Effective critical-service checks now run inside the foreground service.
  Three consecutive failures post diagnostics and close the VPN so Android
  returns to direct routing.
- The production Android engine currently captures explicit `ip_routes` only.
  A full `0.0.0.0/0` TUN experiment correctly exposed domain rules to the
  engine but broke direct egress, so it was rolled back before publication.
  Domain/SNI routing needs a loop-free protected direct outbound before the
  Android agent can be considered feature-equivalent to Windows/Linux.

## 8. Linux Agent

- The Dima scenario has a one-click wrapper: `./one_click_install.sh`.
- The prod package can carry `runtime/sing-box`; use the `-IncludeRuntime`
  package build when DNS/GitHub reachability on the target machine is uncertain.
- DNS restore is covered by a regression test because `resolvectl dns` must
  receive separate server arguments, not one space-separated string.
- The prod bundle contains `QUICKSTART-RU.md` and `status.sh`; failed installs
  automatically print a diagnostic snapshot.
- Status and rollback helpers exist: `./status.sh`, `sudo ./uninstall_systemd.sh`,
  and `sudo ./restore_direct.sh`.
- The package installs an independent systemd watchdog timer that checks the
  effective critical-service list and restores direct routing after repeated
  failure.
- Manual routes should not be required in the normal install path.
- Check conflicts with Amnezia, Zapret, and UFW from real `./status.sh` output.
- Move toward standalone agent behavior similar to Android/Windows.

## 9. UI

- Admin UI: users, devices, routes, provider servers, probe history, status.
- User UI: server choice, Auto, domain-to-candidate-list overrides.
- User UI can save local Auto priority lists for default routing and
  per-domain overrides.
- User auth can be skipped when the user arrives through VPN/agent identity.
- Admin keeps login/password.
- Show statuses: applied, waiting for probe, Auto winner, agent offline.
- Route lookup aliases are editable from user/admin UI and can also be managed
  through `service-alias-list`, `service-alias-set`, and
  `service-alias-delete`.
- Add per-user critical-service health lists to user/admin UI. Agents cache the
  list locally; watchdog failures must be visible as diagnostics and should
  first request route repair/Auto failover before a full emergency stop.

## 10. Cudy

- Roles:
  - LAN-wide agent for the home network;
  - emergency fallback control path.
- Gradually remove heavy business logic from Cudy.
- Keep traffic-drop/load investigations separate from the main control plane.
- First Go milestone: deploy `cudy-fallback` as a loopback-only OpenWrt service
  beside the existing static fallback files, then compare its readiness output
  with `python tools\check_cudy_fallback_status.py --strict`.
- The Go milestone is live: Cudy maintains a dedicated restricted SSH tunnel,
  refreshes the `cudy-home` policy every minute, and keeps a root-only 24-hour
  last-known-good cache. `check_cudy_go_fallback.py --strict` validates both
  services, observer freshness, policy source, routes, and transports.
- The separate Go `cudy-router-agent` is deployed in `observe` mode on Cudy. It
  pulls live/cached policy, renders required transport/dnsmasq/nft artifacts,
  applies file changes transactionally when enabled, and rolls back files and
  newly enabled transport services on failure. Connectivity gates and repeated
  observe diffs remain mandatory before `apply` mode.
- Keep `cudy-fallback` and `cudy-router-agent` as separate processes and failure
  domains. They may share internal Go packages; a multicall binary is optional
  later if OpenWrt flash size requires it.
- Router-agent rollout modes are `disabled`, `observe`, and `apply`. Complete
  an observe/diff phase behind AirTies before Cudy becomes the main DHCP/DNS/WAN
  router.

## 11. Final Verification

- Admin sets global default.
- User sets local override.
- Agent receives policy.
- Auto chooses a winner.
- Traffic goes through the expected provider.
- Verify Windows, Android, and Cudy LAN.
- Verify failures:
  - `uswest` down -> Cudy fallback discovery;
  - provider down -> next candidate.

## 12. Deferred Native AWG Backend

- Implement `cudy-awg-native` only after control-server, Windows, Android,
  Linux, and Cudy fallback behavior is stable.
- It must provide independently named concurrent AWG transports so `aktau`
  and `uswest` can be active at the same time without sharing `AmneziaVPN`.
- Replace the service-wrapper implementation behind the existing transport
  abstraction; control-server policy and route semantics must not change.
