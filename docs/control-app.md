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
  - `Auto` is available as a saved choice.
- Admin page:
  - inspect all servers;
  - edit server labels;
  - enable/disable servers;
  - control user visibility;
  - create users;
  - edit user names, roles, VPN client IPs, status, and default server;
  - reset user passwords;
  - show or hide newly entered password values before saving;
  - add global `domain -> server` routes;
  - add or delete `domain -> server` routes for any user.
- Deploy preview:
  - combines global admin routes and per-user routes;
  - per-user route wins when the same domain exists in both layers;
  - shows the effective plan per VPN client IP.
- PBR export:
  - writes global admin routes to `build/pbr-overrides/force-<interface>.domains`;
  - does not export per-user routes yet because those need source-IP matching on Cudy.
- PBR deploy:
  - previews upload/apply actions by default;
  - creates a Cudy backup before `--apply`;
  - can install the updated PBR and `vpn-switch` scripts with `--install-scripts`.

## Not Implemented Yet

- writing live PBR/OpenWrt rules;
- source-IP deploy for per-user route overrides;
- automatic best-server benchmarking;
- multi-user invitation or remote sync.

Those should be implemented after the local data model stabilizes.
