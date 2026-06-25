# Local Control App

The local control app is the first web layer over the server inventory.

It is intentionally read/write only for local state. It does not modify live Cudy routing yet.

## Initialize

```powershell
python tools\vpn_control_app.py init-db
```

The SQLite database is created at:

```text
data/vpn_control.db
```

The database is ignored by Git.

## User Identity

Normal users do not need a web password when they open the panel through AmneziaVPN. The app can identify them by source VPN IP:

```text
users.client_ip = 10.77.0.x
```

Create the first administrator for local/admin access:

```powershell
python tools\vpn_control_app.py create-user admin --role admin
```

The command asks for the password interactively and stores only a PBKDF2 hash in SQLite.

Import existing Cudy AmneziaVPN clients from local `.conf` files:

```powershell
python tools\vpn_control_app.py import-cudy-clients
```

Sync live Cudy clients from the router:

```powershell
python tools\vpn_control_app.py sync-cudy-clients
```

Create a normal user manually:

```powershell
python tools\vpn_control_app.py create-user alex --role user --display-name "Alex" --client-ip 10.77.0.25 --no-password-change
```

Update an existing user without changing the password:

```powershell
python tools\vpn_control_app.py create-user alex --role user --display-name "Alex" --no-password-change
```

## Run

```powershell
python tools\vpn_control_app.py serve --host 127.0.0.1 --port 8765
```

Open:

```text
http://127.0.0.1:8765/
```

Admin:

```text
http://127.0.0.1:8765/admin
```

## Current Features

- Login page:
  - username/password authentication;
  - `HttpOnly` cookie sessions;
  - PBKDF2 password hashing.
- User page:
  - choose default server;
  - add or delete `domain -> server` routes;
  - `Auto` is available as a saved choice;
  - when `Auto` is selected, save user-local comma-separated candidate priority
    lists such as `proxyde, proxynl, all-rest` for default routing or a specific
    domain.
- Admin page:
  - inspect all servers;
  - edit server labels;
  - enable/disable servers;
  - control user visibility;
  - create users;
  - create Cudy AmneziaWG clients and download generated `.conf` profiles;
  - delete users and optionally revoke their Cudy peer;
  - edit user names, roles, VPN client IPs, status, and default server;
  - reset user passwords;
  - show or hide newly entered password values before saving;
  - add global `domain -> server` routes;
  - add or delete `domain -> server` routes for any user;
  - review unknown domains discovered by route lookup and mark them as
    `pending`, `reviewed`, `ignored`, or `promoted`;
  - inspect and edit cached `Auto` choices.
- Auto cache:
  - stores `domain -> selected server` decisions in `domain_auto_cache`;
  - lets `Auto` routes resolve to a real interface during route plan/export;
  - can be edited from the admin UI or the `auto-cache-*` CLI commands.
- Auto priority policies:
  - store ordered server candidates for Auto probing as part of default/domain route configuration;
  - support global default, global per-domain, user default, and user per-domain scopes;
  - resolve in this order: user domain, user default, global domain, global default.
- Auto selection:
  - probes candidate servers from Cudy with `curl --interface`;
  - chooses the fastest successful candidate for the requested domain;
  - can save the result into `domain_auto_cache` from CLI or the admin UI;
  - can optionally deploy routes after saving.
- Domain discovery:
  - `route-lookup` records unknown domain targets that resolve to `Direct`;
  - discovery is advisory only and does not create a route or change live traffic;
  - admin UI can review the queue and prefill a global route form with the
    discovered domain;
  - admin UI can also explicitly promote a discovered domain into a global
    `domain -> auto` route, optionally with a comma-separated priority list;
  - CLI commands:

```powershell
python tools\vpn_control_app.py domain-discovery-list
python tools\vpn_control_app.py domain-discovery-mark example.com reviewed
python tools\vpn_control_app.py domain-discovery-promote example.com --candidates "proxyde, proxynl, all-rest"
```

- Deploy preview:
  - combines global admin routes and per-user routes;
  - per-user route wins when the same domain exists in both layers;
  - shows the effective plan per VPN client IP;
  - shows a read-only combined deploy preview for the global PBR and per-user source-IP layers.
- PBR export:
  - writes global admin routes to `build/pbr-overrides/force-<interface>.domains`;
  - does not export per-user routes yet because those need source-IP matching on Cudy.
- PBR deploy:
  - previews upload/apply actions by default;
  - creates a Cudy backup before `--apply`;
  - can install the updated PBR and `vpn-switch` scripts with `--install-scripts`.
- User-route deploy:
  - exports enabled per-user routes to `build/user-routes/routes.tsv`;
  - applies source-IP nft rules through `/usr/bin/cudy-user-routes-apply`;
  - derives PBR marks from existing `ip rule show` output on Cudy;
  - exposes deployed nft counters through `status-user-routes`.
- Combined deploy:
  - `deploy-routes` previews or applies both global and per-user route layers in order;
  - the admin page can apply the same combined route deployment when the web server has a local Cudy SSH password.

## Not Implemented Yet

- background refresh of cached `Auto` choices;
- fully automatic route creation from discovered domains without admin review;
- multi-user invitation or remote sync.

Those should be implemented after the local data model stabilizes.
