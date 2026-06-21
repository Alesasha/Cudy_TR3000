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
| Provider refresh | Existing Cudy-side scripts are preserved and can be triggered by CLI. | `python tools\vpn_inventory.py refresh-provider all` | Verify Cudy cron/service schedule and run an apply test when safe. |
| Runtime Cudy snapshot | Implemented as SSH inventory collection. | `python tools\vpn_inventory.py refresh-cudy` | Add this to the regular operational checklist or future service. |
| User/admin web UI | Implemented locally in Python. | `python tools\vpn_control_app.py serve --host 127.0.0.1 --port 8765` | Move to Cudy after Go port or expose through a hardened local route. |
| Cudy client lifecycle | Implemented for create/download/delete/sync through `friendctl`. | Admin UI and `python tools\vpn_control_app.py sync-cudy-clients` | Add regression tests around duplicate users and revoked peers. |
| Effective route plan | Implemented. User routes override global routes. | `python tools\vpn_control_app.py route-plan` | Add automated assertions for conflict cases. |
| Global route deploy | Implemented through PBR override files. | `python tools\vpn_control_app.py deploy-routes` | Apply only after checking generated preview. |
| Per-user route deploy | Implemented through `cudy_user_routes` nft table. | `python tools\vpn_control_app.py deploy-routes` and `python tools\vpn_control_app.py status-user-routes` | Re-apply after router reboot until the Cudy-side startup behavior is verified. |
| Auto priority policies | Implemented as ordered policies consumed by Auto selection. | `python tools\vpn_control_app.py auto-candidates-list` | Add more production policies. |
| Auto selection | Implemented as Cudy-side `curl --interface` probes with fastest successful winner. | `python tools\vpn_control_app.py auto-select example.com --candidates "proxyde, proxyus, uswest"` | Add richer content checks for services like Gemini. |
| Auto cache | Implemented as manually editable cache and Auto selection output. | `python tools\vpn_control_app.py auto-cache-list` | Add automatic refresh of leaders. |
| Auto default for unknown domains | Not implemented. Unknown domains follow normal Cudy/PBR routing. | `python tools\vpn_control_app.py route-plan` | Add domain discovery and an explicit policy for creating Auto routes. |
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

## Domain Discovery Target Behavior

The current system only deploys rules for known domains. The next stage should
collect candidate domains from DNS or routing logs, normalize them, and place
them into a reviewable queue before adding Auto routes.

Minimum useful fields:

- domain;
- first seen time;
- last seen time;
- client IP or user ID;
- hit count;
- current chosen server;
- decision source: global, user, Auto cache, or normal route.

## Go Port Gate

The Go rewrite should start only when these are true:

- smoke check passes locally and with `--online`;
- at least one real per-user route is applied and confirmed by nft counters;
- provider refresh preview works for LokVPN and VPNtype;
- Auto priority policies and Auto cache have a tested end-to-end example;
- the expected behavior for unknown domains is explicitly chosen.
