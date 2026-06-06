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

## OpenWrt Deployment Artifacts

OpenWrt/Cudy scripts live in `openwrt/`. They are source artifacts, not an automatic deployment system yet. Treat changes to PBR, firewall, and live route switching as operational changes requiring a backup and a rollback plan.
