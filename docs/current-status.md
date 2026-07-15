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
- `cudy-router-agent` is running in `observe`. Three consecutive samples kept
  the same 22 routes, 11 pending file changes, 2 transport actions and zero
  policy blockers without changing the workstation egress.
- Provider refresh cron entries exist for VPNtype and LokVPN.
- A live Auto probe rejected `proxyde` for `example.com` and selected `uswest`.
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
- `cudy-router-agent` apply remains blocked by its critical-service preflight:
  production policy currently sends ChatGPT and Gemini through `proxyde`, while
  that transport times out from Cudy. The observer is stable, but strict health
  must pass before apply can be considered.
- The Windows managed-agent task is disabled on the development workstation;
  current traffic is intentionally handled by Cudy instead.
- Android release `1.19 (20)` is installed on the physical phone, but its VPN
  foreground service is currently inactive. Full domain/SNI routing remains a
  known gap.
- The Linux agent needs a longer real-world soak test covering suspend/resume,
  Wi-Fi changes, Zapret and UFW.
- Production reports four enabled agents as offline or stale and has 21 pending
  probe jobs. This is advisory rather than a control-server readiness failure,
  but it must be reconciled during platform acceptance.

## Next Order Of Work

1. Exercise primary-down and primary-recovery through Cudy fallback discovery.
2. Finish dynamic provider lifecycle and content-aware Auto checks.
3. Complete Windows, Android and Linux production acceptance separately.
4. Audit admin/user UI against the current policy model.
5. Prepare a staged, reversible migration from AirTies to Cudy as main router.

Do not enable router-agent apply mode or move DHCP/WAN ownership to Cudy until
fallback recovery and critical-service transport checks have passed.
