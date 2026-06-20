# vpn_control_app.py

`vpn_control_app.py` is the local SQLite-backed control panel for users, domain routes, Auto cache, Auto candidate lists, and Cudy route deployment.

It currently runs on the operator Windows machine, not on the Cudy router. Cudy
has a DHCP static lease for this PC:

```text
vpn-control-pc = 74:56:3c:d5:e1:92 = 192.168.8.102
```

## Start The Web Panel

```powershell
cd C:\Users\Alexander\Cudy_TR3000
python tools\vpn_control_app.py serve --host 0.0.0.0 --port 8765
```

Open:

```text
http://127.0.0.1:8765/
http://127.0.0.1:8765/admin
```

VPN clients connected through Cudy open:

```text
http://192.168.8.102:8765/
http://192.168.8.102:8765/admin
```

Health check:

```powershell
Invoke-WebRequest -UseBasicParsing http://192.168.8.102:8765/healthz
```

## Local Autostart

The Windows Scheduled Task `Cudy VPN Control Web` starts the panel after Windows
login through:

```powershell
C:\Users\Alexander\Cudy_TR3000\tools\start-vpn-control-app.ps1
```

The helper script exits without starting a duplicate server when port `8765` is
already listening.

## Database

Default database:

```text
C:\Users\Alexander\Cudy_TR3000\data\vpn_control.db
```

Initialize or update schema:

```powershell
python tools\vpn_control_app.py init-db
python tools\vpn_control_app.py summary
```

## Users And Cudy Clients

Create an admin:

```powershell
python tools\vpn_control_app.py create-user admin --role admin
```

Sync live Cudy AmneziaWG clients from the router:

```powershell
python tools\vpn_control_app.py sync-cudy-clients
```

Import users from local Cudy `.conf` files only:

```powershell
python tools\vpn_control_app.py import-cudy-clients
```

The admin UI can also create Cudy clients, download their `.conf`, delete users, and optionally revoke Cudy peers.

## Agent Devices

Agent devices are used by the future Linux/Windows/Android route agents. Each
device has its own token; the token is shown only once and only its hash is
stored in SQLite.

Create or rotate a device token:

```powershell
python tools\vpn_control_app.py device-create DC_via_Cudy --platform linux --display-name "Dima Linux"
```

List and revoke devices:

```powershell
python tools\vpn_control_app.py device-list
python tools\vpn_control_app.py device-status
python tools\vpn_control_app.py device-revoke DC_via_Cudy-linux-a1b2c3
```

Fetch the agent config from a running server:

```powershell
$env:DEVICE_TOKEN = '<token shown by device-create>'
Invoke-RestMethod -Headers @{ Authorization = "Bearer $env:DEVICE_TOKEN" } `
  http://127.0.0.1:8765/api/agent/config
```

Send a minimal status report:

```powershell
Invoke-RestMethod -Method Post `
  -Headers @{ Authorization = "Bearer $env:DEVICE_TOKEN" } `
  -ContentType 'application/json' `
  -Body '{"schema_version":1,"platform":"linux","agent_version":"dev","health":{"ok":true}}' `
  http://127.0.0.1:8765/api/agent/status
```

## Routing

Preview effective routes:

```powershell
python tools\vpn_control_app.py route-plan
```

Preview combined deploy:

```powershell
python tools\vpn_control_app.py deploy-routes
```

Apply combined deploy:

```powershell
python tools\vpn_control_app.py deploy-routes --apply
```

The admin UI has the same `Apply Routes` action. The Cudy SSH password is read from `CUDY_SSH_PASSWORD` or from ignored local file:

```text
secrets\cudy_ssh_password.txt
```

Per-user domain routes:

```powershell
python tools\vpn_control_app.py user-domain-route-list
python tools\vpn_control_app.py user-domain-route-set DC_via_Cudy speedtest.net aktau
python tools\vpn_control_app.py user-domain-route-delete DC_via_Cudy speedtest.net
python tools\vpn_control_app.py deploy-user-routes --apply --install-script
```

Per-user IPv4/CIDR routes:

```powershell
python tools\vpn_control_app.py user-ip-route-list
python tools\vpn_control_app.py user-ip-route-set DC_via_Cudy 149.154.160.0/20 aktau
python tools\vpn_control_app.py user-ip-route-delete DC_via_Cudy 149.154.160.0/20
python tools\vpn_control_app.py deploy-user-routes --apply --install-script
```

These routes are exported to the same Cudy `routes.tsv` as per-user domain routes.

## Route Lookup And Aliases

The web UI includes a route lookup tool. Enter an IP, CIDR, URL, domain, or
service alias and it shows the current control-plane decision:

- matched user/global domain or IP rule;
- resolved server for `auto` routes from the Auto cache;
- candidate list that would be used for Auto;
- `direct` when no managed route currently matches.

The same lookup is available from the CLI:

```powershell
python tools\vpn_control_app.py route-lookup telegram --user-id isasha_R7_Cudy
python tools\vpn_control_app.py route-lookup https://ifconfig.me/ip --user-id isasha_R7_Cudy --json
```

When Auto probe jobs include an HTTP URL with enough response body to measure,
new results also store `speed_download` and `speed_mbps`. TCP probes, such as
Telegram CIDR reachability checks, report latency only.

Built-in aliases include:

```text
telegram, tg, телеграм
youtube, yt, ютуб
```

Aliases are editable from the user and admin UI. Each alias expands to a
comma-separated list of domains, IPv4 addresses, or CIDRs. For example, the
Telegram alias expands to the known Telegram IPv4 CIDR set.

## Route Testing

Measure selected Cudy exits from the router itself:

```powershell
python tools\vpn_route_test.py --servers aktau,uswest,proxyru,proxyde --urls https://ifconfig.me/ip,https://www.speedtest.net/
```

Add a rough download-throughput URL when needed:

```powershell
python tools\vpn_route_test.py --servers aktau,uswest --download-url "https://speed.cloudflare.com/__down?bytes=10000000" --csv build\route-tests\latest.csv
```

The test uses `curl --interface` over SSH on Cudy, so it measures the selected OpenWrt interface rather than the operator PC route.

## Dynamic LokVPN Slots

LokVPN profiles are logical choices such as `lokvpn-de1` or `lokvpn-fr2`.
Dynamic slots create independent Cudy TUN interfaces such as `lok1` or `lok2`
for selected LokVPN profiles.

Install or refresh the OpenWrt slot scripts:

```powershell
python tools\lokvpn_slots.py list --install-scripts
```

Create a slot for a profile:

```powershell
python tools\lokvpn_slots.py ensure lokvpn-de1 --install-scripts
```

List current slots:

```powershell
python tools\lokvpn_slots.py list
```

Remove unused slots by keeping only selected profiles:

```powershell
python tools\lokvpn_slots.py gc lokvpn-de1,lokvpn-fr2
```

Remove all current slots:

```powershell
python tools\lokvpn_slots.py gc
```

Remove one slot explicitly:

```powershell
python tools\lokvpn_slots.py remove lok1
```

This is the prototype for the future Go slot manager. Routing rules should keep
storing logical server ids such as `lokvpn-de1`; the runtime layer maps them to
current slot interfaces.

## Provider Transport Refresh

The control server stores agent-ready provider transports in SQLite
`transport_configs`. Managed agents normally consume these configs through
`transport_plan`; local provider API refresh on the agent remains only a
fallback/debug mode.

Secrets are read from environment variables or ignored local files:

```text
VPNTYPE_AUTH_DEFAULT or secrets/vpntype_auth.txt
VPNTYPE_UUID_DEFAULT or secrets/vpntype_uuid.txt
LOKVPN_SUB_URL/SUB_URL or secrets/lokvpn_sub_url.txt
```

Refresh all VPNtype HTTP-proxy TUN configs and all LokVPN VLESS Reality TUN
configs:

```powershell
python tools\vpn_control_app.py provider-refresh all
```

Refresh only selected exits:

```powershell
python tools\vpn_control_app.py provider-refresh vpntype --servers proxyde,proxyru
python tools\vpn_control_app.py provider-refresh lokvpn --servers lokvpn-de1,lokvpn-fr2
```

The admin UI has the same controls in `Provider Transports`: it shows the
agent-facing transport rows, endpoints, source/version, update time, and can run
a manual provider refresh.

`serve` starts provider refresh by default and repeats every 15 minutes:

```powershell
python tools\vpn_control_app.py serve --host 127.0.0.1 --port 8765 --provider-refresh-interval 900
```

Use `--no-provider-refresh-worker` only when testing without provider secrets.

## Auto

Manual Auto cache:

```powershell
python tools\vpn_control_app.py auto-cache-list
python tools\vpn_control_app.py auto-cache-set example.com proxyde --score-ms 120
python tools\vpn_control_app.py auto-cache-delete example.com
```

Ordered candidate lists:

```powershell
python tools\vpn_control_app.py auto-candidates-list
python tools\vpn_control_app.py auto-candidates-set "proxyde, proxyus, uswest"
python tools\vpn_control_app.py auto-candidates-set "proxygb, proxyde" --domain example.com
python tools\vpn_control_app.py auto-candidates-set "proxynl, proxyde" --user-id test-client-awg --domain example.com
```

Probe candidates for a domain from Cudy and save the winner into Auto cache:

```powershell
python tools\vpn_control_app.py auto-select example.com --apply
```

Probe with a temporary candidate list and deploy routes after saving:

```powershell
python tools\vpn_control_app.py auto-select example.com --candidates "proxyde, proxyus, uswest" --apply --deploy
```

LokVPN profile candidates require live profile switching on the shared `lokvpn`
interface:

```powershell
python tools\vpn_control_app.py auto-select example.com --candidates "lokvpn-de1, lokvpn-fr2" --switch-profiles --apply
```

The admin UI exposes the same action in the `Auto Cache` section through `Run Auto`.

Agent-side Auto probes are queued on the control server and executed by managed
agents. This is the production path because the probe runs from the user's
network position rather than from Cudy or from the US control server.

Create and inspect a test probe job:

```powershell
python tools\vpn_control_app.py probe-job-create ifconfig.me proxyde --assigned-device-id isasha_R7_Cudy-windows --url https://ifconfig.me/ip
python tools\vpn_control_app.py probe-job-list
```

Run the scheduler once. It creates at most a small batch of due probe jobs for
enabled `auto` domain routes when Auto cache is missing or stale:

```powershell
python tools\vpn_control_app.py auto-worker-once --max-jobs 5 --cache-ttl-seconds 3600
```

`serve` starts the scheduler by default. Use `--no-auto-worker` only for
debugging:

```powershell
python tools\vpn_control_app.py serve --host 127.0.0.1 --port 8765 --auto-worker-interval 300
```
