# Verification Matrix

This document records verified behaviour across the Python control-server,
platform agents and compact Go services on Cudy. It is both a regression matrix
and the gate for enabling riskier OpenWrt apply/main-router stages.

## Quick Smoke Check

Run local read-only checks:

```powershell
python tools\vpn_smoke_check.py
```

Run local checks plus SSH-based Cudy checks:

```powershell
python tools\vpn_smoke_check.py --online
```

The smoke check does not apply route changes and does not refresh providers with
`--apply`.

## Readiness Matrix

| Area | Current status | Verification command | Remaining work |
| --- | --- | --- | --- |
| Static server inventory | Implemented. Includes own exits, VPNtype, LokVPN profiles, and `Auto`. | `python tools\vpn_inventory.py validate` and `python tools\vpn_inventory.py list` | Keep inventory in sync with real provider scripts. |
| Provider refresh | Existing Cudy-side scripts are preserved, can be triggered by CLI, and the Cudy cron schedule is machine-checked from the runtime snapshot. | `python tools\vpn_inventory.py refresh-provider all` and `python tools\vpn_inventory.py check-provider-schedule` | Run an apply test only when intentionally refreshing live provider endpoints. |
| Runtime Cudy snapshot | Implemented as SSH inventory collection. | `python tools\vpn_inventory.py refresh-cudy` | Add this to the regular operational checklist or future service. |
| User/admin web UI | Deployed on the `uswest` control-server and reachable to agents through the restricted SSH control tunnel. HTTP smoke covers user/enrollment/device lifecycle and immediate token invalidation. User deletion explicitly separates account-only removal from legacy Cudy peer revocation; device controls use reversible state and explicit deletion actions. Production desktop and 375-pixel mobile rendering has no page-level overflow or console errors. | `python tools\test_control_server_http.py`, production `/healthz`/`/readyz`, and an agent-local `http://127.0.0.1:18765/` check | Complete the remaining current-model lifecycle/UX audit and add automated rendered desktop/mobile regression coverage. |
| Cudy client lifecycle | Implemented for create/download/delete/sync through `friendctl`; duplicate local imports and local delete/config cleanup are covered by regression tests. | Admin UI, `python tools\vpn_control_app.py sync-cudy-clients`, and `python tools\test_cudy_client_lifecycle.py` | Add a live revoke test only during a planned Cudy maintenance window. |
| Effective route plan | Implemented. User routes override global routes. | `python tools\vpn_control_app.py route-plan` | Add automated assertions for conflict cases. |
| Global route deploy | Implemented through PBR override files. | `python tools\vpn_control_app.py deploy-routes` | Apply only after checking generated preview. |
| Per-user route deploy | Implemented through `cudy_user_routes` nft table. Domain and IP/CIDR routes with `server_id=auto` are resolved through `domain_auto_cache` before export, so Cudy fallback/LAN follows the current Auto winner. | `python tools\vpn_control_app.py deploy-routes` and `python tools\vpn_control_app.py status-user-routes` | Re-apply after router reboot until the Cudy-side startup behavior is verified. |
| Auto priority policies | Implemented as ordered policies consumed by Auto selection. | `python tools\vpn_control_app.py auto-candidates-list` | Add more production policies. |
| Auto selection | Implemented as agent-side probes with fastest content-valid winner. Python and Go probes reject known geo-block response text and configured success/failure regexes. Important Service routing groups can share one cache key, candidate list and winner. Default apex probes that all candidates report as unresolvable use a 24-hour negative cache, so valid suffix routes do not create continuous failures. Winner history includes recent failed jobs with per-candidate reason, latency and HTTP status. | `python tools\vpn_control_app.py auto-select example.com --candidates "proxyde, proxyus, uswest"`, `python tools\test_probe_semantics.py`, `python tools\test_auto_policy_priority.py`, and `go test ./cmd/cudy-router-agent` | Add browser-rendered checks for JavaScript-only decisions. |
| Auto cache | Implemented as manually editable cache and Auto selection output. | `python tools\vpn_control_app.py auto-cache-list` | Add automatic refresh of leaders. |
| Agent transport plan | Implemented as a minimal control-server plan: applied route exits plus pending probe candidates only. Android additionally keeps recently probed transports warm for six hours (bounded to 64 ids, above the current inventory size) to avoid sliding-window unified-TUN reload churn. Windows applies routes in one bounded PowerShell batch, has an emergency stop/direct-restore command, and successfully applied cached policy with control deliberately unavailable on 2026-07-13. | `python tools\test_auto_policy_priority.py`, `python tools\test_route_agent_plan.py`, `python tools\test_windows_agent_packaging.py`, and `python tools\vpn_smoke_check.py` | Reinstall the live Windows task with `-NoDirectTransports`; in the final platform phase replace AWG wrappers on Windows, Linux, Android and OpenWrt/Cudy with the shared native multi-instance contract. |
| Android agent | Release `1.24 (25)` is built, signed, installed on the physical MIUI test phone and published on production with matching APK/manifest SHA256. Full IPv4 TUN capture, selective FakeIP DNS, protected direct/provider sockets, per-device restricted SSH control, policy fetch, status posting and non-disruptive production probes are working. Admin-generated QR/file provisioning pins the SSH host key and does not embed a shared key in the APK. The in-app mobile admin implements authenticated user/device create, edit, enable/disable, delete and enrollment over the active restricted SSH connection. | `python tools\test_android_agent_ui.py`, `python tools\test_control_server_http.py`, Android Debug/Release builds, `adb shell dumpsys activity services com.nashvpn.cudyagent`, UIAutomator inspection, and local/remote APK manifest/SHA verification | Complete a live mobile-admin CRUD/enrollment cycle, then run a multi-day soak on a second daily-use phone and add rendered probes. |
| Windows safety watchdog | Implemented independently of the managed agent. It tracks route-apply heartbeat, general HTTPS reachability, and a local list of critical services; repeated failures queue/post diagnostics and invoke the full emergency stop. The development workstation additionally requires the Codex backend path. | `powershell -ExecutionPolicy Bypass -File tools\agent-windows\Watch-AgentConnectivity.ps1 -ProbeOnly` and `python tools\test_windows_agent_packaging.py` | Add the same critical-service policy contract to Linux, Android, OpenWrt, and control-server supervision without making ChatGPT a global default. |
| Linux agent packaging | Production `1.23 (24)` is published. The update path verifies that systemd becomes active before reporting completion, and the watchdog repairs an enabled-but-inactive service. The one-click package includes UI/status/diagnostics, direct rollback and bundled sing-box runtime. | `python tools\test_linux_agent_packaging.py` and `powershell -ExecutionPolicy Bypass -File tools\Build-LinuxAgentPackage.ps1 -AgentId DC_via_Cudy-linux -IncludeRuntime` | Bring Dima's currently stale device online, confirm `1.23 (24)`, then complete the 24-48 hour suspend/resume, Wi-Fi, Zapret, UFW and update soak. |
| Auto probe assignment | Implemented. Worker prefers an active probing-capable agent that already reported the target domain, but provider-transport probe jobs require agents with `can_manage_transports=true`, so a Linux client that cannot start managed exits is not assigned those jobs. | `python tools\test_auto_policy_priority.py` | Add production probe policies for more high-value domains. |
| Auto default for unknown domains | Unknown domains still follow `Direct`, but `route-lookup` records direct domain hits into a review queue. Admin can explicitly promote a reviewed domain into `domain -> auto` and optionally queue an immediate Auto probe job. | `python tools\test_domain_discovery.py`, `python tools\vpn_control_app.py domain-discovery-list`, and `python tools\vpn_control_app.py domain-discovery-promote example.com --candidates "proxyde, all-rest" --probe-now` | Decide later whether any domains may be promoted automatically without admin review. |
| Route lookup aliases | Implemented in user/admin UI and CLI. Global aliases are admin-owned; a per-user alias can override the same name only for its owner. Aliases expand to domains, IPs, or CIDRs and `Direct` is reported when no rule matches. | `python tools\test_control_server_http.py`, `python tools\test_service_alias_cli.py`, `python tools\vpn_control_app.py service-alias-list`, and `python tools\vpn_control_app.py route-lookup telegram --user-id isasha_X7Pro_Cudy` | Add production aliases as needed. |
| Control backup/fallback artifacts | Implemented. Fallback sync stores secret control-state under Cudy `/root` and public freshness/endpoint metadata under `/www/cudy-control`. | `python tools\test_control_backup_artifacts.py`, `python tools\check_cudy_fallback_status.py --strict`, and `python tools\vpn_smoke_check.py --online` | Periodically test a full clone to a disposable VPS. |
| Private control SSH recovery | Verified through Cudy to uswest host bridge `172.29.172.1`. A systemd timer discovers the current AmneziaWG container address and repairs the host return route every minute. Deliberately deleting the route proved automatic recovery before a successful private SSH check. | `python tools\install_control_private_management.py --check-only` and `python tools\recover_uswest_ssh_via_cudy.py --private-host 172.29.172.1 --check-only` | Re-run after replacing the uswest host or inbound AWG container/network names. |
| Cudy Go fallback runtime | `cmd/cudy-fallback` is deployed on Cudy. It serves endpoint/state artifacts and readiness/runtime/agent-preview APIs, while a restricted persistent SSH tunnel keeps live control available. | `C:\Users\Alexander\sdk\go1.26.4\Go\bin\go.exe test ./cmd/cudy-fallback`, `powershell -ExecutionPolicy Bypass -File tools\Build-CudyFallbackGo.ps1`, and `python tools\check_cudy_go_fallback.py --strict` | Exercise full primary-down/fallback recovery before router cutover. |
| OpenWrt router agent | The separate Go `cudy-router-agent` is deployed in `observe` mode and does not own PBR, DHCP or WAN. The first live transport-bootstrap trial found that `nohup` is absent on OpenWrt, so its external guard did not arm and manual rollback was required. Rollback restored all backed-up files byte-for-byte; observer, PBR and Direct/provider probes recovered. The replacement uses BusyBox `start-stop-daemon` plus a mandatory `armed` handshake and passed a detached no-routing proof, but has not yet had a second live routing trial. HTTP-proxy health checks use the upstream proxy from the root-only cached transport plan because router-local traffic cannot reliably validate the gVisor TUN. | `C:\Users\Alexander\sdk\go1.26.4\Go\bin\go.exe test ./cmd/cudy-router-agent`, `python tools\test_trial_cudy_transport_bootstrap.py`, `python tools\check_cudy_go_fallback.py --host 192.168.8.1 --strict`, `python tools\check_cudy_router_agent.py --host 192.168.8.1 --expected-mode observe --strict`, and both guarded trial previews | In an explicit maintenance window, re-run the automatic-rollback transport bootstrap, then its committed trial and the two override-only apply trials. Do not combine them with provider refresh or main-router cutover. |
| Domain/IP lists needing tunnel | Static override files exist in `openwrt/pbr-overrides`. | Inspect `openwrt\pbr-overrides\*` and Cudy `/etc/pbr-overrides`. | Implement automatic discovery/update and a review workflow. |

## Auto Selection Target Behavior

`Auto` should become a real resolver, not only a stored placeholder.

1. For a requested domain, resolve candidate servers in this order:
   - user-specific domain list;
   - global domain list;
   - user default list;
   - global default list.
2. Test candidates from Cudy with `curl --interface`.
3. Select the fastest healthy server.
4. Save the winner into `domain_auto_cache`.
5. Re-check cached domains in the background, keeping approximately the last
   300 active domains.

## Provider Refresh Target Behavior

LokVPN and VPNtype refresh must remain router-side until the Go port has a
replacement service.

Safe preview:

```powershell
python tools\vpn_inventory.py refresh-provider all
```

Apply only when intentionally changing live provider endpoints:

```powershell
python tools\vpn_inventory.py refresh-provider all --apply
```

## Domain Discovery Behavior

The current system only deploys rules for known domains. `route-lookup` now
collects unknown domain targets that resolve to `Direct`, normalizes them, and
places them into a reviewable queue before adding Auto routes.

Minimum useful fields:

- domain;
- first seen time;
- last seen time;
- client IP or user ID;
- hit count;
- source, currently `route_lookup` or `manual`;
- review status: `pending`, `reviewed`, `ignored`, or `promoted`.

This queue is deliberately non-invasive: discovered domains do not become live
routes until an admin creates an explicit global or user domain route. Promotion
can be done from the admin UI or the CLI:

```powershell
python tools\vpn_control_app.py domain-discovery-promote example.com --candidates "proxyde, proxynl, all-rest" --probe-now
```

## Go Port Gate

The Go rewrite should start only when these are true:

- smoke check passes locally and with `--online`;
- at least one real per-user route is applied and confirmed by nft counters;
- provider refresh preview works for LokVPN and VPNtype;
- Auto priority policies and Auto cache have a tested end-to-end example;
- the expected behavior for unknown domains is explicitly chosen.
- the initial `cmd/cudy-fallback` binary is deployed as a Cudy service and its
  `/readyz` agrees with the existing static fallback status check.
