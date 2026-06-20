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
  last probes, and backup age. The first machine-readable API is `/api/status`.

## 3. Fallback Control Path

- Keep Cudy as an emergency fallback control path.
- Sync a lightweight endpoint manifest from primary to Cudy.
- Sync a secret full control-state archive from primary to Cudy under `/root`,
  with only a non-secret freshness status under `/www`.
- Agents should know a primary URL, optional direct fallback URLs, and static
  endpoint manifest URLs.
- If `uswest` is rebuilt or moved, update the Cudy manifest first; agents can
  discover the new primary through Cudy.
- Windows agent can already read the Cudy manifest and switch the SSH tunnel
  host for the next control connection.
- Later, replace the static manifest with a compact Go fallback service on Cudy
  that can serve minimal policy, not only endpoint discovery.

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

## 5. Provider Transports

- Control-server is the normal source of fresh LokVPN/VPNtype transport data.
- Agents receive ready `transport_plan` entries and should not regularly call
  provider APIs.
- Agent-side provider refresh remains only as fallback.
- Start only needed LokVPN/VPNtype transports; stop unused slots immediately.

## 6. Windows Agent

- Production install/uninstall.
- Self-heal SSH tunnel.
- Self-heal sing-box transports.
- Clear logs and status command.
- Smoke-test after Windows reboot.
- Verify Telegram, Gemini, ifconfig, speedtest, and direct traffic.

## 7. Android Agent

- Prepare a normal user instruction.
- Verify behavior after phone reboot.
- Document battery restrictions and foreground service behavior.
- Add production probe-job support, not only debug probes.
- Build release APK with clear name and version.

## 8. Linux Agent

- Return after Windows/Android are stable.
- Make the Dima scenario one-click.
- Avoid manual routes.
- Check conflicts with Amnezia, Zapret, and UFW.
- Move toward standalone agent behavior similar to Android/Windows.

## 9. UI

- Admin UI: users, devices, routes, provider servers, probe history, status.
- User UI: server choice, Auto, domain-to-candidate-list overrides.
- User auth can be skipped when the user arrives through VPN/agent identity.
- Admin keeps login/password.
- Show statuses: applied, waiting for probe, Auto winner, agent offline.

## 10. Cudy

- Roles:
  - LAN-wide agent for the home network;
  - emergency fallback control path.
- Gradually remove heavy business logic from Cudy.
- Keep traffic-drop/load investigations separate from the main control plane.

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
