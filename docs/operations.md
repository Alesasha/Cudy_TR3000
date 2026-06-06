# Operations

## Add AmneziaWG Client

Use:

```powershell
python tools\awg_client_add.py <server> <client-name>
```

Known direct servers:

```powershell
python tools\awg_client_add.py --list-servers
```

Examples:

```powershell
python tools\awg_client_add.py cudy-home phone-alex
python tools\awg_client_add.py hostvds-uswest phone-alex
python tools\awg_client_add.py megahost-aktau phone-alex
```

Use `--dry-run` to read server state and show the planned client profile path without modifying the remote server.

## Client Statistics

All configured servers:

```powershell
python tools\awg_client_add.py all --stats
```

Single server:

```powershell
python tools\awg_client_add.py cudy-home --stats
```

## Inventory

Validate and list:

```powershell
python tools\vpn_inventory.py validate
python tools\vpn_inventory.py list
python tools\vpn_inventory.py admin-list --include-disabled
```

Refresh Cudy runtime snapshot:

```powershell
$env:CUDY_SSH_PASSWORD = '<router password>'
python tools\vpn_inventory.py refresh-cudy
Remove-Item Env:CUDY_SSH_PASSWORD
```

## Local Control App

Initialize local SQLite state:

```powershell
python tools\vpn_control_app.py init-db
```

Create the first administrator:

```powershell
python tools\vpn_control_app.py create-user admin --role admin
```

Create a normal user:

```powershell
python tools\vpn_control_app.py create-user user1 --role user --display-name "User 1"
```

Run the local web UI:

```powershell
python tools\vpn_control_app.py serve --host 127.0.0.1 --port 8765
```

Open:

```text
http://127.0.0.1:8765/
http://127.0.0.1:8765/admin
```

The admin page can create normal users, change their passwords, enable or disable them, and edit their domain routes.

## OpenWrt Deployment Artifacts

OpenWrt/Cudy scripts live in `openwrt/`. They are source artifacts, not an automatic deployment system yet. Treat changes to PBR, firewall, and live route switching as operational changes requiring a backup and a rollback plan.
