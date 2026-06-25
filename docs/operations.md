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

Sync live Cudy users directly from `friendctl` on the router and download their `.conf` files:

```powershell
python tools\vpn_control_app.py sync-cudy-clients
```

The admin `Users` section also has a `Sync Cudy` button. Use it when users exist on the Cudy router but are not visible in the control panel.

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

Per-user domain routes can be managed from CLI without opening the admin panel:

```powershell
python tools\vpn_control_app.py user-domain-route-list
python tools\vpn_control_app.py user-domain-route-set DC_via_Cudy speedtest.net aktau
python tools\vpn_control_app.py user-domain-route-delete DC_via_Cudy speedtest.net
python tools\vpn_control_app.py deploy-user-routes --apply --install-script
```

Per-user routes can also target literal IPv4/CIDR ranges. This is useful for services such as Telegram where the routing decision is IP-list based, not domain based:

```powershell
python tools\vpn_control_app.py user-ip-route-list
python tools\vpn_control_app.py user-ip-route-set DC_via_Cudy 149.154.160.0/20 aktau
python tools\vpn_control_app.py user-ip-route-delete DC_via_Cudy 149.154.160.0/20
python tools\vpn_control_app.py deploy-user-routes --apply --install-script
```

The route target is stored as normalized IPv4/CIDR and exported into the same `/etc/cudy-user-routes/routes.tsv` file as domain routes. The Cudy apply script accepts both domains and IPv4/CIDR values in the `target` column.

Check deployed per-user route counters:

```powershell
$env:CUDY_SSH_PASSWORD = '<router password>'
python tools\vpn_control_app.py status-user-routes
Remove-Item Env:CUDY_SSH_PASSWORD
```

After the matching user opens the test domain, the nft rule should show `counter packets ... bytes ...`.

Deploy all route layers together:

```powershell
python tools\vpn_control_app.py deploy-routes
```

Apply all route layers from PowerShell:

```powershell
$env:CUDY_SSH_PASSWORD = '<router password>'
python tools\vpn_control_app.py deploy-routes --apply
Remove-Item Env:CUDY_SSH_PASSWORD
```

`deploy-routes` is the preferred operator command after editing routes in the panel. It applies the global PBR layer only when needed, then applies the per-user source-IP layer. Add `--install-scripts` after changing the OpenWrt scripts in this repository. The separate `deploy-pbr-overrides` and `deploy-user-routes` commands remain available for targeted maintenance.

The same deployment can also be launched from the admin panel with `Apply Routes`. The web server reads the Cudy SSH password from `CUDY_SSH_PASSWORD` or from the ignored local file `secrets/cudy_ssh_password.txt`. The browser never receives the password.

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

To create a new external Cudy client from the admin page, keep `Create Cudy VPN .conf` checked in the user form. The server calls `/usr/bin/friendctl add` on Cudy, imports the assigned `10.77.0.x` address into the user record, and saves the generated profile under `secrets/clients/cudy-home/`. After creation, use the returned `Download .conf` link or the row `Config` button.

The user row `Delete` button can revoke the Cudy peer and delete the local user. If remote revoke is skipped, only the local control-plane user and local saved config are removed; the remote peer can still keep working until revoked on Cudy. After deleting a user with domain routes, use `Apply Routes` to remove that user's source-IP rules from the live Cudy nft table.

The admin `Deploy Preview` block can refresh the effective route plan, refresh the combined deploy dry-run plan, and apply the current plan to Cudy.

## Harden Control-Server SSH

The public control-server SSH port is exposed for operators and roaming agents,
so it receives constant bot traffic. Apply the managed SSH hardening profile:

```powershell
$env:USWEST_SSH_PASSWORD = (Get-Content secrets\control_backup_ssh_password.txt -Raw).Trim()
python tools\harden_control_ssh.py
Remove-Item Env:USWEST_SSH_PASSWORD
```

The tool writes `/etc/ssh/sshd_config.d/99-cudy-anti-bruteforce.conf`, reloads
`sshd`, installs or updates the `sshd` fail2ban jail, and prints the top SSH
source IPs from the last six hours. Current defaults:

```text
LoginGraceTime 60
PerSourceMaxStartups 20
MaxStartups 100:30:300
UseDNS no
fail2ban: maxretry=5, findtime=10m, bantime=1h
```

## Autostart

Cudy/OpenWrt services should come back after a Cudy reboot when their init scripts are enabled. On the current router, these services are enabled:

- `network`;
- `pbr`;
- `sing-box`;
- `firewall`;
- `cron`.

The inbound Cudy AmneziaWG server is the UCI interface `network.awg_in`, so OpenWrt `network` brings it up. Provider refresh is cron-driven, but it must stay off the hot path. Refreshing LokVPN or all VPNtype proxies can restart sing-box/netifd interfaces, which makes PBR temporarily disable forwarding while it rebuilds routing. Keep these jobs at night unless the refresh scripts become change-aware and non-disruptive.

```text
7 5 * * * /usr/bin/vpntype-proxy-refresh-all
29 5 * * * /usr/bin/lokvpn-refresh-current
```

The local control panel is different: `vpn_control_app.py serve` runs on this Windows machine. It does not automatically start after a Windows reboot unless a Windows Scheduled Task or service wrapper starts it.

Manage cached `Auto` choices:

```powershell
python tools\vpn_control_app.py auto-cache-list
python tools\vpn_control_app.py auto-cache-set example.com proxyde --score-ms 120
python tools\vpn_control_app.py auto-cache-delete example.com
```

When a global or per-user route points to `auto`, export/deploy resolves it through `domain_auto_cache`. If there is no cached selected server for that domain, the route is skipped and the deploy preview shows a warning.

Manage Auto priority policies for future Auto benchmarks:

```powershell
python tools\vpn_control_app.py auto-candidates-list
python tools\vpn_control_app.py auto-candidates-set "proxyde, proxyus, all-rest"
python tools\vpn_control_app.py auto-candidates-set "proxygb, proxyde, all-rest" --domain example.com
python tools\vpn_control_app.py auto-candidates-set "proxynl, proxyde, all-rest" --user-id test-client-awg --domain example.com
python tools\vpn_control_app.py auto-candidates-delete --domain example.com
python tools\vpn_control_app.py auto-winners telegram
```

Blank `--user-id` means a global policy. Blank `--domain` means the default policy for all domains. Candidate policy resolution order is user+domain, user default, global+domain, global default. `all-rest` expands to every remaining enabled user-visible server after the explicitly listed priorities.

`Auto` is not currently applied to every unknown domain automatically. The current deploy model only emits rules for domains present in global or per-user route tables; unknown domains keep following normal Cudy/PBR routing until a discovery or benchmark flow adds them.

## Linux Agent Package

Rebuild the Linux prod package with the bundled `sing-box` runtime when the
target machine may have broken DNS or slow GitHub access:

```powershell
powershell -ExecutionPolicy Bypass -File tools\Build-LinuxAgentPackage.ps1 -AgentId DC_via_Cudy-linux -IncludeRuntime
```

The resulting archive is written under `secrets\agents\*-linux-prod.zip`.
The regression check for this path is:

```powershell
python tools\test_linux_agent_packaging.py
```

## OpenWrt Deployment Artifacts

OpenWrt/Cudy scripts live in `openwrt/`. They are source artifacts, not an automatic deployment system yet. Treat changes to PBR, firewall, and live route switching as operational changes requiring a backup and a rollback plan.
