# Verification Before Go Port

This document defines the behavior that should be proven in the Python version
before moving the control plane to a compact Go binary on Cudy.

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
| User/admin web UI | Implemented locally in Python. | `python tools\vpn_control_app.py serve --host 127.0.0.1 --port 8765` | Move to Cudy after Go port or expose through a hardened local route. |
| Cudy client lifecycle | Implemented for create/download/delete/sync through `friendctl`; duplicate local imports and local delete/config cleanup are covered by regression tests. | Admin UI, `python tools\vpn_control_app.py sync-cudy-clients`, and `python tools\test_cudy_client_lifecycle.py` | Add a live revoke test only during a planned Cudy maintenance window. |
| Effective route plan | Implemented. User routes override global routes. | `python tools\vpn_control_app.py route-plan` | Add automated assertions for conflict cases. |
| Global route deploy | Implemented through PBR override files. | `python tools\vpn_control_app.py deploy-routes` | Apply only after checking generated preview. |
| Per-user route deploy | Implemented through `cudy_user_routes` nft table. | `python tools\vpn_control_app.py deploy-routes` and `python tools\vpn_control_app.py status-user-routes` | Re-apply after router reboot until the Cudy-side startup behavior is verified. |
| Auto priority policies | Implemented as ordered policies consumed by Auto selection. | `python tools\vpn_control_app.py auto-candidates-list` | Add more production policies. |
| Auto selection | Implemented as Cudy-side `curl --interface` probes with fastest successful winner. | `python tools\vpn_control_app.py auto-select example.com --candidates "proxyde, proxyus, uswest"` | Add richer content checks for services like Gemini. |
| Auto cache | Implemented as manually editable cache and Auto selection output. | `python tools\vpn_control_app.py auto-cache-list` | Add automatic refresh of leaders. |
| Agent transport plan | Implemented as a minimal control-server plan: applied route exits plus pending probe candidates only. | `python tools\test_auto_policy_priority.py` and `python tools\vpn_smoke_check.py` | Continue checking real Windows agent after reboot because stale local processes require elevated cleanup. |
| Auto probe assignment | Implemented. Worker prefers an active probing-capable agent that already reported the target domain, then falls back to another agent for that user. | `python tools\test_auto_policy_priority.py` | Add production probe policies for more high-value domains. |
| Auto default for unknown domains | Unknown domains still follow `Direct`, but `route-lookup` records direct domain hits into a review queue. Admin can explicitly promote a reviewed domain into `domain -> auto` and optionally queue an immediate Auto probe job. | `python tools\test_domain_discovery.py`, `python tools\vpn_control_app.py domain-discovery-list`, and `python tools\vpn_control_app.py domain-discovery-promote example.com --candidates "proxyde, all-rest" --probe-now` | Decide later whether any domains may be promoted automatically without admin review. |
| Route lookup aliases | Implemented in user/admin UI and CLI. Aliases expand to domains, IPs, or CIDRs and `Direct` is reported when no rule matches. | `python tools\test_service_alias_cli.py`, `python tools\vpn_control_app.py service-alias-list`, and `python tools\vpn_control_app.py route-lookup telegram --user-id isasha_X7Pro_Cudy` | Add production aliases as needed. |
| Control backup/fallback artifacts | Implemented. Fallback sync stores secret control-state under Cudy `/root` and public freshness/endpoint metadata under `/www/cudy-control`. | `python tools\test_control_backup_artifacts.py`, `python tools\check_cudy_fallback_status.py --strict`, and `python tools\vpn_smoke_check.py --online` | Periodically test a full clone to a disposable VPS. |
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
