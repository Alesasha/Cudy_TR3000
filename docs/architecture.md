# Architecture

## Goal

The project turns a Cudy TR3000/OpenWrt router into a managed VPN/proxy routing hub.

Users should eventually be able to open a simple local web UI, choose a routing exit from the available servers, or use `Auto`. Administrators should be able to inspect and edit all servers, provider profiles, users, and per-user domain routing rules.

AmneziaVPN/AmneziaWG remains the user transport. The control app does not replace the VPN client; it binds a web user to the VPN client IP and decides how Cudy should route that user's traffic.

The next control-plane step is to run the same control app on a public host,
starting with `uswest`. Client agents will authenticate with per-device tokens,
pull desired routing state over HTTPS, and apply local split-routing decisions.
Cudy remains one possible exit and deployment target, but it is no longer the
only place where routing decisions can be executed.

## Current Routing Layers

1. Cudy is the central router.
2. PBR decides which outbound interface receives selected traffic.
3. Own AmneziaWG exits are native interfaces:
   - `awg1`: Megahost Aktau.
   - `awg2`: HostVDS US West.
4. VPNtype HTTP proxy exits are represented as sing-box TUN interfaces:
   - `proxygb`, `proxyca`, `proxyfr`, `proxyby`, `proxyae`, `proxyhk`, `proxykz`, `proxytr`, `proxyil`, `proxycz`, `proxypl`, `proxyfi`, `proxynl`, `proxyal`, `proxyru`, `proxyus`, `proxyde`.
5. LokVPN started as one sing-box selector interface, but the target model is dynamic slots:
   - `lokvpn` with profiles `smart1`, `de1`, `ru1`, `nl1`, `fr1`, `se1`, `smart2`, `de2`, `ru2`, `nl2`, `fr2`, `se2`.
   - Runtime slots such as `lok1`, `lok2`, ... can be created on demand for individual profiles.
   - User/admin rules keep logical ids such as `lokvpn-de1`; the runtime layer maps them to active slot interfaces.
   - Unused slots are removed by garbage collection instead of staying resident.

## Public Control Server And Agents

The public control server owns desired state:

- users and devices;
- global and per-user domain rules;
- per-user IP/CIDR rules;
- Auto cache and priority policies;
- last reported agent status.

Client agents pull this state from `GET /api/agent/config` using a device token
and report health through `POST /api/agent/status`.

Initial agents can run beside AmneziaVPN on Linux and Windows by managing local
routes around the VPN interface. Android likely needs a first-party VPN client
based on `VpnService` because Android does not let one ordinary app reliably
modify another VPN app's routes.

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

Effective routing is built per user from two layers:

- global admin `domain -> server` routes;
- user-specific `domain -> server` routes.

If both layers contain the same domain, the user-specific route wins.

The first deploy artifact is intentionally narrower: global admin routes can be exported as `/etc/pbr-overrides/force-<interface>.domains` files because they map cleanly to existing destination-IP PBR sets. User-specific routes need source-IP matching and should not be collapsed into global destination sets.

The local deploy command is conservative: it previews by default, backs up `/etc/pbr-overrides` and the PBR user script before apply, and uploads only non-empty generated domain files unless `--prune-empty` is specified.

User-specific routes are deployed through a separate nft table, `inet cudy_user_routes`. Its prerouting chain runs after the normal PBR mangle chain and sets the PBR mark for packets matching both source client IP and resolved destination IP. This keeps per-user overrides out of the global destination-only PBR sets.

Provider endpoint refresh stays on Cudy during this stage. LokVPN and VPNtype keep using their existing router-side scripts and cron jobs, while the local project inventories them and can trigger them over SSH through `tools/vpn_inventory.py refresh-provider --apply`.

## Dynamic LokVPN Slot Model

The slot manager is intentionally runtime-only. It should not make users choose
`lok1` or `lok2`; those names are implementation details.

```text
user route: DC_via_Cudy / example.com -> lokvpn-de1
runtime:    lokvpn-de1 -> lok1
Cudy:       nft/PBR mark sends matching packets to interface lok1
```

Slot lifecycle:

1. A slot is created when a rule, Auto winner, or probe needs a LokVPN profile.
2. Existing slots are reused when their profile is requested again.
3. A slot with no logical rules and no active probe is removed by GC.
4. There is no fixed pool size; operational limits should be enforced as soft
   warnings before hard caps.

Current prototype commands live in `tools/lokvpn_slots.py` and install
`/usr/bin/lokvpn-slot` plus the updated `/usr/bin/lokvpn-refresh` on Cudy.

Stage 4 starts by making `Auto` resolvable through `domain_auto_cache`: a route can keep `server_id = auto`, while export/deploy expands it to the cached concrete server for that domain. The first implementation allows an administrator to edit this cache manually from the UI or CLI.

`Auto` is not currently a wildcard for every domain with no rule. The deploy model only emits rules for known global or per-user domains. Unknown domains follow the normal Cudy/PBR routing path until they are added to a route, cache, or future domain-discovery flow.

Auto priority policies define which servers a benchmark should try and in what priority order. Resolution order is:

1. user + domain;
2. user default;
3. global + domain;
4. global default.

The remaining work is to benchmark exits per domain, keep roughly 300 active domains, refresh cached leaders in the background, and optionally discover new domains from DNS/PBR logs before creating `Auto` routes.
