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
  check observed live policy with 22 routes and 9 transports.
- `cudy-router-agent` is running in `observe`. It receives live policy, reports
  agent heartbeat, processes Auto probe jobs and passes its strict check with
  22 routes and five healthy critical services. It still does not modify PBR.
- Provider refresh cron entries exist for VPNtype and LokVPN.
- Content-aware Auto probes now run on the requesting agent. From Cudy,
  `gemini.google.com` selected `proxyde` at 1394 ms, while `chatgpt.com`
  rejected four timing-out provider exits and selected `uswest` at 1050 ms.
  A matching ChatGPT domain candidate policy now permits that measured winner.
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

## Incomplete Or Degraded

- Historical SSH banner failures remain a condition to monitor, but they are
  not currently reproducible. The production audit needed a 60-second timeout
  because its complete remote status check can legitimately exceed 30 seconds.
- `cudy-router-agent` apply remains intentionally disabled. Strict preflight is
  now green, but the current preview still contains seven PBR file changes and
  needs a controlled reversible acceptance window before any apply trial.
- The Windows managed-agent task is disabled on the development workstation;
  current traffic is intentionally handled by Cudy instead.
- Android release `1.19 (20)` is installed on the physical phone, but its VPN
  foreground service is currently inactive. Full domain/SNI routing remains a
  known gap.
- The Linux agent needs a longer real-world soak test covering suspend/resume,
  Wi-Fi changes, Zapret and UFW.
- Production currently reports one of four enabled agents online. The old Auto
  backlog is being drained by Cudy; recent failed probes are warnings rather
  than a control-server readiness failure and must be reconciled during
  platform acceptance.

## Next Order Of Work

1. Soak the Cudy observer and reconcile the remaining Auto probe backlog.
2. Complete Windows, Android and Linux production acceptance separately.
3. Audit admin/user UI against the current policy model.
4. Run a controlled rollback-tested Cudy apply trial without moving DHCP/WAN.
5. Prepare a staged, reversible migration from AirTies to Cudy as main router.

Do not enable router-agent apply mode or move DHCP/WAN ownership to Cudy until
fallback recovery and critical-service transport checks have passed.
