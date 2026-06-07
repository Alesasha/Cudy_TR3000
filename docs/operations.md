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

Preview provider refresh commands that would run on Cudy:

```powershell
python tools\vpn_inventory.py refresh-provider
python tools\vpn_inventory.py refresh-provider vpntype
python tools\vpn_inventory.py refresh-provider lokvpn --profile fr2
python tools\vpn_inventory.py refresh-provider proxyde
```

Run the existing Cudy refresh scripts explicitly:

```powershell
$env:CUDY_SSH_PASSWORD = '<router password>'
python tools\vpn_inventory.py refresh-provider --apply
Remove-Item Env:CUDY_SSH_PASSWORD
```

`refresh-provider` is a wrapper around the scripts already installed on Cudy. It does not replace the router cron job or store provider API secrets in the local project.

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
python tools\vpn_control_app.py create-user user1 --role user --display-name "User 1" --client-ip 10.77.0.25 --no-password-change
```

Import existing Cudy AmneziaVPN users:

```powershell
python tools\vpn_control_app.py import-cudy-clients
```

Preview effective per-user routing:

```powershell
python tools\vpn_control_app.py route-plan
python tools\vpn_control_app.py route-plan --json
```

Export global admin routes into PBR override files:

```powershell
python tools\vpn_control_app.py export-pbr-overrides
python tools\vpn_control_app.py export-pbr-overrides --json
```

The files are written under `build/pbr-overrides/` and are intended for `/etc/pbr-overrides/` on Cudy. This export only includes global admin routes; per-user routes require source-IP nft/PBR rules and are handled in the next deploy layer.

Preview deploy to Cudy:

```powershell
python tools\vpn_control_app.py deploy-pbr-overrides
python tools\vpn_control_app.py deploy-pbr-overrides --install-scripts
```

Apply deploy to Cudy:

```powershell
$env:CUDY_SSH_PASSWORD = '<router password>'
python tools\vpn_control_app.py deploy-pbr-overrides --apply --install-scripts
Remove-Item Env:CUDY_SSH_PASSWORD
```

The deploy command creates a backup under `/root/backup-pbr-overrides/` before uploading files. By default it uploads only non-empty generated `force-<interface>.domains` files and `manifest.json`; add `--prune-empty` only when empty generated files should clear older generated routes. If there are zero global routes, `--apply` requires `--allow-empty`.

Export and preview per-user source-IP routes:

```powershell
python tools\vpn_control_app.py export-user-routes
python tools\vpn_control_app.py deploy-user-routes --install-script
```

Apply per-user source-IP routes to Cudy:

```powershell
$env:CUDY_SSH_PASSWORD = '<router password>'
python tools\vpn_control_app.py deploy-user-routes --apply --install-script
Remove-Item Env:CUDY_SSH_PASSWORD
```

This creates `/etc/cudy-user-routes/routes.tsv` and installs `/usr/bin/cudy-user-routes-apply`. The apply script reads PBR marks from `ip rule show`, creates nft rules matching `ip saddr <client_ip> ip daddr <resolved-domain-ip>`, and sets the corresponding PBR mark. If there are zero user routes, `--apply` requires `--allow-empty`.

Run the local web UI:

```powershell
python tools\vpn_control_app.py serve --host 127.0.0.1 --port 8765
```

Open:

```text
http://127.0.0.1:8765/
http://127.0.0.1:8765/admin
```

The admin page can create normal users, bind them to VPN client IPs, change passwords if needed, enable or disable them, edit global routes, and edit per-user domain routes.

## OpenWrt Deployment Artifacts

OpenWrt/Cudy scripts live in `openwrt/`. They are source artifacts, not an automatic deployment system yet. Treat changes to PBR, firewall, and live route switching as operational changes requiring a backup and a rollback plan.
