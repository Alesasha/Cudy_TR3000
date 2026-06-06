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

- User page:
  - choose default server;
  - add or delete `domain -> server` routes;
  - `Auto` is available as a saved choice.
- Admin page:
  - inspect all servers;
  - edit server labels;
  - enable/disable servers;
  - control user visibility;
  - inspect users and domain route table.

## Not Implemented Yet

- authentication;
- writing live PBR/OpenWrt rules;
- automatic best-server benchmarking;
- multi-user invitation or remote sync.

Those should be implemented after the local data model stabilizes.
