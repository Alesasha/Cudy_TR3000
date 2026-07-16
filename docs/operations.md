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
`sshd`, installs or updates the `cudy-sshd-safe` fail2ban filter/jail, installs
`cudy-sshd-watchdog.timer`, installs `cudy-ssh-firewall-guard.service`, and
prints the top SSH source IPs from the last six hours. The fail2ban filter is intentionally conservative: it bans obvious
brute-force lines such as invalid users and failed passwords, but ignores likely
agent reconnect noise for `cudy-tunnel-windows` and `cudy-tunnel-linux`, plus
banner/timeout/reset lines. The watchdog runs every minute and kills only stale
pre-auth/banner SSH children older than the configured threshold; it does not
match active `user@pts/...` sessions. The firewall guard is intentionally
lighter than a ban: it limits excessive new SSH connections per source before
they create more `sshd` pre-auth children, while allowing normal roaming agent
reconnects. Current defaults:

```text
LoginGraceTime 60
PerSourceMaxStartups 20
MaxStartups 100:30:300
UseDNS no
fail2ban: filter=cudy-sshd-safe, banaction=iptables-multiport, maxretry=5, findtime=10m, bantime=1h
cudy-sshd-watchdog: stale=120s, interval=60s
cudy-ssh-firewall-guard: at most 32 concurrent SSH connections / source IP
```

The firewall guard deliberately does not rate-limit new SSH connections over
time. Multiple roaming agents can share one carrier or home NAT address and can
legitimately reconnect together after a network interruption. Authentication
failures are handled by the conservative fail2ban filter; global pre-auth load
is bounded by OpenSSH `MaxStartups` and the stale-session watchdog.

Do not run many direct root SSH checks or deploys in parallel. The control
server accepts multiple sessions from the same source IP (`MaxSessions=100`,
`PerSourceMaxStartups=20`), but repeated failed banner/auth attempts can still
leave pre-auth children until `LoginGraceTime` expires. Local scheduled jobs
should use low retry counts and prefer the existing control tunnel or the Cudy
fallback state where possible.

Useful checks after deployment:

```bash
systemctl status cudy-sshd-watchdog.timer cudy-sshd-watchdog.service
systemctl status cudy-ssh-firewall-guard.service
journalctl -t cudy-sshd-watchdog -S '1 hour ago' --no-pager
iptables -S CUDY-SSH-GUARD
ss -Htn sport = :22 | wc -l
```

If public SSH is reachable at TCP level but new sessions fail before the SSH
banner while an existing control tunnel still works, a private management path
may be used only after a dedicated private SSH address has been configured and
verified on uswest. Run this from an elevated PowerShell window and pass that
address explicitly:

```powershell
powershell -ExecutionPolicy Bypass -File tools\recover_uswest_ssh_via_awg.ps1 `
  -PrivateSshHost <verified-private-management-ip>
```

The script starts a temporary `UswestAdmin` AWG tunnel from
`secrets\agents\isasha_R7_Cudy-windows\uswest-awg.conf`, connects to the
explicit management address, installs the same hardening/watchdog profile, and
then rechecks public SSH. AWG peer `10.8.1.1` belongs to a client and must not be
used as a server management address. Until a dedicated address is provisioned,
the valid recovery paths are the provider console/reboot and the current Cudy
fallback control state.

Once that management address exists, the same path can be used through Cudy's
already-running `awg2` interface without starting a Windows AWG tunnel:

```powershell
python tools\recover_uswest_ssh_via_cudy.py `
  --private-host <verified-private-management-ip> `
  --check-only
```

Remove `--check-only` only after the private connectivity check succeeds. The
tool adds one narrow `/32` route on Cudy and never assumes a management address.

On the current uswest layout, AmneziaWG runs in Docker: host bridge `amn0` is
`172.29.172.1`, container `amnezia-awg2` is attached to
`amnezia-dns-net`, and its `awg0` client subnet is `10.8.1.0/24`. Install the
idempotent host return-route and its one-minute repair timer with:

```powershell
python tools\install_control_private_management.py
python tools\recover_uswest_ssh_via_cudy.py `
  --private-host 172.29.172.1 `
  --check-only
```

The installer discovers the container address from Docker on every run and
does not modify firewall, NAT, SSH authentication, VPN keys or policy routes.
If Amnezia recreates the Docker network with a different host bridge address,
read the installer's `PRIVATE_MANAGEMENT_HOST` output and pass the new verified
address explicitly.

## Guarded Cudy Transport Bootstrap

Before any live Cudy routing or transport change, move the operator/control
path off Cudy. On the Windows development workstation, keep the AmneziaVPN APP
connected, connect Wi-Fi directly to the main router, and arm the maintenance
guard from an elevated PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File tools\Start-CudyMaintenanceGuard.ps1
```

The guard pins the active AmneziaVPN endpoint to the Wi-Fi gateway, verifies
that OpenAI remains reachable through that tunnel, verifies that Cudy
management still resolves over Ethernet, and runs a hidden repair loop. It
refuses to arm if any prerequisite is missing. A specific saved Wi-Fi profile
or tunnel endpoint can be supplied with `-WifiProfile` and `-TunnelEndpoint`.
Disarm it after the maintenance window:

```powershell
powershell -ExecutionPolicy Bypass -File tools\Stop-CudyMaintenanceGuard.ps1
```

This out-of-band guard protects the operator session only. It is not evidence
that Cudy PBR works and must not be counted as a router acceptance check.

The first router-agent apply trial is intentionally override-only. If its
preview reports `transport_actions`, prepare those exits in a separate guarded
transaction first:

```powershell
python tools\trial_cudy_transport_bootstrap.py --host 192.168.8.1
```

The preview is read-only. An actual trial requires `--apply --yes`; retaining a
successful trial additionally requires `--commit`. The tool backs up the
transport configs, init scripts, service state and `/etc/config/pbr` on Cudy,
then arms an on-router rollback before stopping the observer. The Go binary's
`prepare` mode is one-shot only and requires `-allow-transport-prepare`; it
starts the required exits, registers their PBR interfaces, rebuilds the current
PBR state and verifies every provider through its upstream proxy. It does not
write the new domain/IP override files.

The rollback is detached with OpenWrt's BusyBox `start-stop-daemon`; the tool
waits for an `armed` marker written by the rollback process before it is allowed
to stop the observer. Do not replace this with `nohup`: that command is absent
on the current Cudy firmware.

For the first live exercise, omit `--commit` and wait for automatic rollback.
Only after checking restored service/PBR state should a second, separately
committed transport bootstrap be run. Then repeat the override-only preview:

```powershell
python tools\trial_cudy_router_agent_apply.py --host 192.168.8.1
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

The Go router-agent is the exception to the old script-only model, but its
deployed service remains read-only:

```powershell
python tools\check_cudy_go_fallback.py --strict
python tools\check_cudy_router_agent.py --strict
```

Both checks must pass before any router apply test. A router-agent warning about
a missing interface is acceptable only when `desired.json` contains a matching
validated `prepare-and-start` transport action and has no blockers.

## Windows OpenAI-only recovery transport

The operator workstation can keep the AmneziaVPN application disabled and use
a dedicated standalone AmneziaWG service only for OpenAI. The start/stop and
route refresh scripts are documented in `tools/agent-windows/README.md`.

The expected dedicated-tunnel state is:

- the AmneziaVPN GUI is disconnected and `AmneziaWGTunnel$AmneziaVPN` is absent;
- `AmneziaVPN-service` may remain running so the GUI is available as a manual fallback;
- `AmneziaWGTunnel$OpenAI-USWest` is running and starts automatically;
- `Cudy OpenAI Route Refresh` runs at startup and every two minutes;
- the AWG endpoint has a physical-gateway `/32` pin stored in maintenance state;
- the AWG interface has OpenAI `/32` routes but no default or `/1` routes;
- Direct destinations continue through the physical Ethernet default route.

Connecting the AmneziaVPN GUI is treated as an explicit full-tunnel override:
the scheduled refresh suspends `OpenAI-USWest` to avoid nesting one VPN inside
the other, then resumes it after the application tunnel is disconnected.
