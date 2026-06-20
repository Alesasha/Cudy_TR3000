# Public Control Server

The public control server is the next deployment mode for `tools/vpn_control_app.py`.
The first target host is `uswest` (`95.182.91.203`).

Its job is control-plane only:

- store users, devices, domain rules, Auto cache, and candidate policies;
- expose the admin/user web UI;
- expose agent APIs for Linux/Windows/Android clients;
- later trigger Cudy and exit-server deployments.

It is not required to carry user traffic. User traffic can still go direct,
through Cudy, or through any selected exit.

## Agent API

Agents authenticate with a device token. The token is shown only once when the
device is created. Only a PBKDF2 hash is stored in SQLite.

Create or rotate a token:

```powershell
python tools\vpn_control_app.py device-create DC_via_Cudy --platform linux --display-name "Dima Linux"
```

List devices:

```powershell
python tools\vpn_control_app.py device-list
python tools\vpn_control_app.py device-status
```

Revoke a device:

```powershell
python tools\vpn_control_app.py device-revoke DC_via_Cudy-linux-a1b2c3
```

Fetch agent config:

```bash
curl -sS \
  -H "Authorization: Bearer $DEVICE_TOKEN" \
  https://control.example.net/api/agent/config
```

Send status:

```bash
curl -sS \
  -H "Authorization: Bearer $DEVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"schema_version":1,"platform":"linux","agent_version":"dev","health":{"ok":true}}' \
  https://control.example.net/api/agent/status
```

For local testing without TLS:

```powershell
python tools\vpn_control_app.py serve --host 127.0.0.1 --port 8765
```

Then use `http://127.0.0.1:8765/api/agent/config`.

## First Uswest Deployment Shape

For the first live test, use SSH tunneling and keep Python bound to localhost:

```text
operator/client PC -> ssh -L -> uswest 127.0.0.1:8765 -> vpn_control_app.py
```

Minimum server-side files:

```text
/opt/cudy-control/
  config/vpn_inventory.json
  data/vpn_control.db
  tools/vpn_control_app.py
  deploy/uswest/vpn-control.service
  deploy/uswest/Caddyfile.example
```

The service command stays simple for the Python MVP:

```bash
python3 /opt/cudy-control/tools/vpn_control_app.py serve --host 127.0.0.1 --port 8765
```

Systemd template:

```bash
sudo cp /opt/cudy-control/deploy/uswest/vpn-control.service /etc/systemd/system/vpn-control.service
sudo systemctl daemon-reload
sudo systemctl enable --now vpn-control
sudo systemctl status vpn-control
```

Open the panel through an SSH tunnel from the operator PC:

```powershell
ssh -N -L 8765:127.0.0.1:8765 root@95.182.91.203
```

Or use the local helper:

```powershell
.\tools\start-uswest-control-tunnel.ps1
```

Then open locally:

```text
http://127.0.0.1:8765/
http://127.0.0.1:8765/admin
```

For a Linux client agent:

```bash
ssh -N -L 8765:127.0.0.1:8765 root@95.182.91.203
export VPN_CONTROL_URL="http://127.0.0.1:8765"
export VPN_AGENT_TOKEN="vca_..."
python3 tools/route_agent.py plan --post-status
```

This avoids public HTTP and avoids the need to know each client's changing IP.

## One-Click Clone To A New VPS

This tool assumes the target VPS already exists, has Ubuntu installed by the
provider, and accepts root SSH. Selecting/reinstalling the OS is still done in
the provider panel or provider API before this script can connect.

Prepare a fresh Ubuntu/Debian target after SSH is available:

```powershell
$env:TARGET_SSH_PASSWORD = "<target root password>"
python tools\bootstrap_control_vps.py --host <new-vps-ip> --hostname cudy-control-2
Remove-Item Env:TARGET_SSH_PASSWORD
```

The bootstrap installs Python, SQLite, curl, tar, Docker, Docker Compose plugin,
and creates `/opt/cudy-control` with the `cudy-control` system user. It does not
install or overwrite Amnezia server configuration.

The control-server can be cloned from the current uswest host to a replacement
VPS with:

```powershell
$env:SOURCE_SSH_PASSWORD = "<source root password>"
$env:TARGET_SSH_PASSWORD = "<target root password>"
python tools\clone_control_server.py --target-host <new-vps-ip>
Remove-Item Env:SOURCE_SSH_PASSWORD,Env:TARGET_SSH_PASSWORD
```

The clone copies the whole remote `/opt/cudy-control` tree, including:

- `data/vpn_control.db`;
- provider refresh secrets;
- agent device token hashes and status;
- Auto cache, probe history, routes, aliases, and transport configs;
- deployed code, inventory, docs, and systemd unit.

By default the source service is stopped briefly while the archive is created,
then started again. This gives a consistent SQLite/WAL copy. Use
`--no-stop-source` only when brief source downtime is worse than the risk of an
inconsistent live database copy.

The script installs Python/curl/tar on the target when `apt-get` is available,
creates the `cudy-control` system user, installs `vpn-control.service`, starts
the service, and checks `http://127.0.0.1:8765/healthz`.

After a clone to a different IP:

- update operator SSH tunnels to point at the new IP;
- update agent SSH host settings or regenerate agent bundles;
- only then decommission the old source.

The local transfer archive is deleted after upload by default. `--keep-archive`
is available for debugging, but the archive is sensitive and must not be
committed or shared.

Restore a replacement VPS from an existing local backup archive:

```powershell
$env:TARGET_SSH_PASSWORD = "<target root password>"
python tools\clone_control_server.py `
  --source-archive backups\control-server\cudy-control-95-182-91-203-YYYYMMDD-HHMMSS.tgz `
  --target-host <new-vps-ip>
Remove-Item Env:TARGET_SSH_PASSWORD
```

This is the fallback path when the old control-server is unavailable.

## Backups

Create a local disaster-recovery archive from the live uswest control-server:

```powershell
$env:CONTROL_BACKUP_SSH_PASSWORD = "<root password>"
python tools\backup_control_server.py
Remove-Item Env:CONTROL_BACKUP_SSH_PASSWORD
```

The backup uses SQLite's online backup API, so `vpn-control` does not need to be
stopped. The archive includes, by default:

- `data/vpn_control.db`;
- `secrets/`;
- `config/`, `deploy/`, `docs/`, `openwrt/`, `tools/`;
- `requirements.txt`;
- `backup-metadata.txt`.

Local backup archives are written to `backups/control-server/` and the newest 10
are kept by default. These files contain secrets and are ignored by git.

Use `--no-secrets` only for a shareable diagnostic archive. A no-secrets backup
is not sufficient for a seamless restore because provider refresh credentials
and agent/transport private material may be missing.

Install a local Windows daily backup task:

```powershell
Set-Content -NoNewline -Path secrets\control_backup_ssh_password.txt -Value "<root password>"
powershell -ExecutionPolicy Bypass -File tools\Install-ControlBackupTask.ps1 -RunNow
```

The scheduled task does not store the SSH password in its command-line
arguments. `tools\Run-ControlBackup.ps1` reads it from
`secrets\control_backup_ssh_password.txt` or from `CONTROL_BACKUP_SSH_PASSWORD`.
The file is under ignored `secrets/` and must not be committed.

Current disaster-recovery layers:

1. Create or reinstall the VPS with Ubuntu in the provider panel/API.
2. Restore the control-server with `clone_control_server.py` from live source or
   from a backup archive.
3. Recreate or migrate the AmneziaWG/exit-server layer separately if the new
   host must also carry traffic.
4. Update operator tunnels and agent bundles to the new control-server host.

The control-server backup covers the control plane. It does not by itself
install a fresh Amnezia server or migrate external provider accounts. That
should be automated as the next disaster-recovery layer.

## Later HTTPS Mode

Use a reverse proxy such as Caddy or nginx when the service needs direct public
access without an SSH tunnel:

```text
internet -> HTTPS reverse proxy -> 127.0.0.1:8765 -> vpn_control_app.py
```

Caddy template:

```bash
sudo cp /opt/cudy-control/deploy/uswest/Caddyfile.example /etc/caddy/Caddyfile
sudo caddy fmt --overwrite /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Replace `control.example.net` with the real DNS name before reloading Caddy.

Before exposing it directly to the internet:

- enable HTTPS;
- keep admin passwords strong;
- use per-device tokens, never shared user passwords;
- back up `data/vpn_control.db`;
- keep `secrets/` and provider credentials outside git.

## Agent Config Model

`GET /api/agent/config` returns:

- `user`: user identity and default server id;
- `device`: authenticated device metadata;
- `control.endpoints`: primary/fallback control endpoint manifest;
- `servers`: user-visible exits;
- `domain_routes`: global routes overridden by user-specific routes;
- `ip_routes`: user-specific IPv4/CIDR routes;
- `transport_plan`: ready-to-run local transport configs required by the current routes;
- `auto_candidates`: candidate server policies;
- `control.reserved_targets`: currently `direct` and `auto`.

The first Linux/Windows agents should treat this as desired state and keep a
local cache so routing can continue when the control server is temporarily
unavailable.

The endpoint manifest is also available without agent auth:

```text
GET /api/control/endpoints
```

Publish the same manifest as a static Cudy fallback file:

```powershell
$env:CUDY_SSH_PASSWORD = "<router password>"
python tools\sync_control_manifest_to_cudy.py
Remove-Item Env:CUDY_SSH_PASSWORD
```

Agents can use the static URLs as discovery fallbacks:

```powershell
$env:VPN_CONTROL_ENDPOINT_MANIFEST_URLS = "http://10.77.0.1/cudy-control/endpoints.json,http://192.168.8.1/cudy-control/endpoints.json"
```

Windows `Start-ManagedAgent.ps1` reads this manifest before opening the SSH
control tunnel. If the manifest advertises a different
`endpoints[].ssh_tunnel.host`, the agent starts the tunnel to that host. This is
the intended migration path after `uswest` is rebuilt or receives a new IP:

```powershell
$env:VPN_CONTROL_PRIMARY_SSH_HOST = "<new-uswest-ip>"
python tools\sync_control_manifest_to_cudy.py
```

## Transport Plan

Provider API work belongs on the control server in normal mode. Agents should
not need VPNtype or LokVPN secrets. The control server stores the latest ready
transport config and sends only the exits needed by the current user's routes.

Set a VPNtype HTTP proxy endpoint:

```powershell
python tools\vpn_control_app.py transport-set-http proxyde `
  --proxy-host 104.194.158.155 `
  --proxy-port 12345 `
  --source vpntype-refresh
```

Set a generic VLESS/Reality or full sing-box config:

```powershell
python tools\vpn_control_app.py transport-set-json lokvpn-de1 vless-reality-tun `
  --config-json "@build\lokvpn-de1-transport.json"
```

List stored transport configs:

```powershell
python tools\vpn_control_app.py transport-list
```

Windows agents consume this with:

```powershell
.\Start-ManagedAgent.ps1 -NoDirectTransports
```

## First Route Agent

The dry-run prototype is `tools/route_agent.py`.

```bash
export VPN_CONTROL_URL="https://control.example.net"
export VPN_AGENT_TOKEN="vca_..."
python3 tools/route_agent.py plan --post-status
```

See `tools/README-route-agent.md` for details.
