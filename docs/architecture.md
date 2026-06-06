# Architecture

## Goal

The project turns a Cudy TR3000/OpenWrt router into a managed VPN/proxy routing hub.

Users should eventually be able to open a simple local web UI, choose a routing exit from the available servers, or use `Auto`. Administrators should be able to inspect and edit all servers, provider profiles, users, and per-user domain routing rules.

## Current Routing Layers

1. Cudy is the central router.
2. PBR decides which outbound interface receives selected traffic.
3. Own AmneziaWG exits are native interfaces:
   - `awg1`: Megahost Aktau.
   - `awg2`: HostVDS US West.
4. VPNtype HTTP proxy exits are represented as sing-box TUN interfaces:
   - `proxygb`, `proxyca`, `proxyfr`, `proxyby`, `proxyae`, `proxyhk`, `proxykz`, `proxytr`, `proxyil`, `proxycz`, `proxypl`, `proxyfi`, `proxynl`, `proxyal`, `proxyru`, `proxyus`, `proxyde`.
5. LokVPN is represented as a sing-box selector interface:
   - `lokvpn` with profiles `smart1`, `de1`, `ru1`, `nl1`, `fr1`, `se1`, `smart2`, `de2`, `ru2`, `nl2`, `fr2`, `se2`.

## Stage 1

Stage 1 creates a trustworthy local inventory:

- static server catalog in `config/vpn_inventory.json`;
- read-only runtime snapshot from Cudy in `config/cudy-runtime.json`;
- CLI access through `tools/vpn_inventory.py`;
- no routing changes and no user-facing web UI yet.

This gives the next stages a clean source of truth instead of embedding provider knowledge into unrelated scripts.

## Next Stages

Stage 2 adds the local database and admin/user model. SQLite is enough at this scale:

- `servers`;
- `provider_profiles`;
- `users`;
- `user_domain_routes`;
- `domain_auto_cache`;
- `health_checks`.

The first implementation is `tools/vpn_control_app.py`. It stores local choices and provides a simple user/admin UI without changing live Cudy routing.

Stage 3 should connect saved choices to live Cudy routing generation and deployment.

Stage 4 should implement `Auto`: benchmark exits per domain, keep a cache of roughly 300 active domains, and refresh cached leaders in the background.
