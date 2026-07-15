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
- Control backup and Cudy fallback sync tasks are installed on Windows.
- Targeted control, Auto, packaging, watchdog, PBR and Go regression tests pass.
- No file under `secrets/` is tracked by Git.

## Incomplete Or Degraded

- Direct SSH to `uswest` remains intermittently slow or unavailable during the
  SSH banner stage. The control API is reachable through the restricted Cudy
  tunnel, but direct production SSH checks are not reliable yet.
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
- The working tree contains a large uncommitted implementation set and must be
  split into reviewed thematic commits before further risky deployment.

## Next Order Of Work

1. Review and commit the current control, agent, Cudy and documentation blocks.
2. Restore `cudy-router-agent` in `observe` and verify repeated stable diffs.
3. Diagnose and harden direct SSH access to `uswest`.
4. Exercise primary-down and primary-recovery through Cudy fallback discovery.
5. Finish dynamic provider lifecycle and content-aware Auto checks.
6. Complete Windows, Android and Linux production acceptance separately.
7. Audit admin/user UI against the current policy model.
8. Prepare a staged, reversible migration from AirTies to Cudy as main router.

Do not enable router-agent apply mode or move DHCP/WAN ownership to Cudy until
steps 1-4 have passed.
