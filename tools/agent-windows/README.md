# Windows Route Agent

This folder is a template for a Windows client package. Put these files in one
folder with `route_agent.py`, rename `agent.env.ps1.example` to
`agent.env.ps1`, and set the device token issued by the control server.

Open PowerShell as Administrator when applying routes.

Use a Windows-friendly AmneziaWG profile. Do not import a profile with:

```ini
AllowedIPs = 0.0.0.0/0
```

On Windows, AmneziaWG treats a single-peer `/0` profile as a kill-switch profile
and installs restrictive firewall rules. For this managed route-agent mode use
the local Cudy profile with:

```ini
Endpoint = 192.168.8.1:51830
AllowedIPs = 0.0.0.0/1, 128.0.0.0/1
DNS = 192.168.8.1
```

If enabling that profile still resets SSH or blocks internet, use the narrower
split profile first. It has no default routes and only allows explicitly listed
tunnel ranges, so normal internet must stay on the physical gateway:

```ini
Endpoint = 192.168.8.1:51830
AllowedIPs = 10.77.0.0/24, 149.154.160.0/20, ...
DNS = 192.168.8.1
```

Do not use `AllowedIPs = 0.0.0.0/0` on Windows, even with `Table = off`.
Amnezia can still install full-tunnel routes and block normal internet.

If internet is lost after enabling a test profile, disconnect Amnezia or run:

```powershell
.\Emergency-Stop-Agent.ps1
```

For an emergency double-click `Emergency-Stop-Agent.cmd`. It requests
Administrator permission, stops and disables the scheduled agent task, stops
the control tunnel and all managed transports, removes their active routes,
and restores the physical default route and DNS. The agent stays disabled
until it is deliberately enabled or reinstalled.

`Restore-Direct.ps1` remains available for a narrower route reset that does not
disable the scheduled task.

## OpenAI-only maintenance AWG

For development recovery, OpenAI can use a dedicated AWG peer without enabling
the AmneziaVPN application or changing the Windows default route. Run the
following from an elevated PowerShell prompt with the dedicated client config:

```powershell
.\Start-OpenAIMaintenanceTunnel.ps1 `
  -ConfigPath "C:\path\to\isasha_R7_OpenAI.conf" `
  -TunnelName "OpenAI-USWest"
```

The script installs the standalone AmneziaWG tunnel service with `Table = off`
and adds only current `/32` routes resolved for the configured OpenAI domains.
`Cudy OpenAI Route Refresh` refreshes those routes every two minutes and at
Windows startup. The default route remains on the physical LAN interface.
If the user explicitly connects the AmneziaVPN application, the refresh task
suspends this dedicated tunnel to prevent nested VPN routing. It resumes the
dedicated tunnel after the application tunnel is disconnected.

To repair only the refresh task for an already running maintenance tunnel,
without reinstalling or interrupting the AWG service, run as Administrator:

```powershell
.\Install-OpenAIMaintenanceRefreshTask.ps1
```

If the endpoint uses a reserve Wi-Fi network that Windows does not reconnect
while Ethernet is active, persist its existing profile as part of the repair:

```powershell
.\Install-OpenAIMaintenanceRefreshTask.ps1 -WiFiProfile "Profile name"
```

Stop and remove this maintenance tunnel with:

```powershell
.\Stop-OpenAIMaintenanceTunnel.ps1 -TunnelName "OpenAI-USWest"
```

This is IP routing. A CDN address shared by OpenAI and another hostname follows
the same `/32` route until the next refresh; domain-exclusive routing requires
an application-aware proxy or WFP implementation.

## Managed Transport PoC

This mode bypasses the Amnezia UI. It starts the AmneziaWG tunnel daemon with
only the `[Interface]` section and `Table = off`, then adds the peer through the
AmneziaWG UAPI named pipe. In this mode `AllowedIPs = 0.0.0.0/0` is only a peer
crypto rule; routes are installed by `route_agent.py`.

The default single transport config path is `client-awg.conf` in this folder.
The current AmneziaVPN Windows service binary has hard-coded service and pipe
names for `AmneziaVPN`, so it supports one active AWG tunnel at a time. For
direct own-server mode, the portable package uses:

```text
aktau-awg.conf  -> tunnel/interface AmneziaVPN
uswest-awg.conf -> available for switching the active direct exit
```

The PoC result is documented in `docs/windows-managed-transport-poc.md`.

Use this only from an elevated PowerShell:

```powershell
.\Restore-Direct.ps1
.\Start-AwgTransport.ps1
.\Check-Net.ps1
```

Expected before applying routes:

```text
1.1.1.1       -> Ethernet
95.182.91.203 -> Ethernet
104.17.147.22 -> Ethernet
```

Then apply policy routes:

```powershell
.\Start-Tunnel.ps1
.\Apply-Routes.ps1 -InterfaceAlias "AmneziaVPN"
.\Check-Net.ps1
```

Expected after applying routes:

```text
1.1.1.1       -> Ethernet
95.182.91.203 -> Ethernet
104.17.147.22 -> AmneziaVPN
149.154.160.1 -> AmneziaVPN
```

## Managed Agent

## Desktop UI

The production package includes a small Windows UI that controls the existing
agent scripts without duplicating routing logic. Install its Desktop and Start
Menu shortcuts with:

```powershell
.\Install-AgentUi.ps1
```

The main action always reflects the current state: `Start agent`, `Starting...`
or `Stop agent`. Stopping uses the full safe shutdown and restores direct
routing. Diagnostics can be viewed and copied in the same window. Update checks
use the authenticated control tunnel; after an update replaces the package the
UI relaunches itself so the displayed version is current. Closing the UI does
not stop the background agent.

The universal installer creates these shortcuts automatically.

After the managed transport PoC is verified, use one long-running command
instead of separate terminals. It starts the AWG transport when needed, keeps
the SSH tunnel to the control server alive, fetches real policy from the
control server, applies routes, and posts status.

Run from an elevated PowerShell. By default this starts direct Aktau AWG and
maps control-server `aktau` rules directly to the local Windows interface:

```powershell
.\Start-ManagedAgent.ps1
```

Normal output is one summary line per cycle. To show the full route-agent
output on every cycle:

```powershell
.\Start-ManagedAgent.ps1 -VerboseRoutes
```

For one cycle only:

```powershell
.\Start-ManagedAgent.ps1 -Once
```

Legacy Cudy transport mode is still available:

```powershell
.\Start-ManagedAgent.ps1 -NoDirectTransports -ServerId aktau -InterfaceAlias "AmneziaVPN"
```

To switch the active direct exit to US West for a test:

```powershell
.\Start-ManagedAgent.ps1 -DirectTransport uswest=AmneziaVPN=uswest-awg.conf -Once
```

## Cudy-Style Sing-Box Transports

For VPNtype and LokVPN, the Windows agent follows the Cudy model: each exit is
an independent `sing-box` process with a TUN interface, and `route_agent.py`
maps `server_id` to that local interface.

Normal mode is control-server driven. `/api/agent/config` returns a
`transport_plan` containing only exits currently needed by that user/device.
The agent writes local sing-box configs, starts those TUN interfaces, then
applies routes.

Put `sing-box.exe` into:

```text
.\runtime\sing-box.exe
```

Or install it from the official SagerNet GitHub release:

```powershell
.\Install-SingBoxRuntime.ps1
```

Run one cycle using only control-server transport_plan:

```powershell
.\Start-ManagedAgent.ps1 -NoDirectTransports -Once
```

Generate a static HTTP-proxy/VPNtype style config for manual testing:

```powershell
.\New-SingBoxHttpProxyConfig.ps1 -Name proxyde -ProxyHost 104.194.158.155 -ProxyPort 12345
```

Fallback mode: let the agent refresh a VPNtype endpoint from the API. Put these variables
into `agent.env.ps1` first:

```powershell
$env:VPNTYPE_AUTH_DEFAULT = "..."
$env:VPNTYPE_UUID_DEFAULT = "..."
```

Then run one cycle with a Cudy-style local `proxyde` TUN interface:

```powershell
.\Start-ManagedAgent.ps1 -NoDirectTransports -VpnTypeTransport proxyde=proxyde -Once
```

Generate a LokVPN profile config from the subscription URL for manual testing:

```powershell
$env:LOKVPN_SUB_URL = "https://..."
.\New-LokVpnConfig.ps1 -Profile de1 -Name lokvpn-de1
```

Fallback mode: let the agent refresh and run the LokVPN transport automatically:

```powershell
.\Start-ManagedAgent.ps1 -NoDirectTransports -LokVpnTransport de1=lokvpn-de1 -Once
```

Run managed agent with Cudy-style local TUN exits:

```powershell
.\Start-ManagedAgent.ps1 `
  -VpnTypeTransport proxyde=proxyde `
  -LokVpnTransport de1=lokvpn-de1
```

This adds mappings equivalent to Cudy PBR interfaces:

```text
proxyde    -> local TUN interface proxyde
lokvpn-de1 -> local TUN interface lokvpn-de1
```

To install autostart at Windows logon:

```powershell
.\Install-ManagedAgentTask.ps1
Start-ScheduledTask -TaskName "Cudy Managed Route Agent"
```

Current production default for this package is control-server driven:

```powershell
.\Install-ManagedAgentTask.ps1 -RunNow
```

This installs a logon task that runs the managed agent with the local package
settings. The current production bundle uses:

```text
Start-ManagedAgent.ps1 -NoDirectTransports -ExtraInterfaceMap aktau=AmneziaVPN
```

That means:

- control-server decides which sing-box transports are needed;
- VPNtype/LokVPN exits are started from `transport_plan` and unused exits are
  stopped automatically;
- existing direct `aktau` rules map to the local AmneziaWG interface named
  `AmneziaVPN`;
- provider API refresh on the Windows agent is fallback-only and is not needed
  in normal production mode;
- the agent keeps the SSH tunnel to `uswest` alive;
- route apply status is posted back to the control-server.

Build a refreshed per-device production ZIP from repository source plus local
secrets:

```powershell
powershell -ExecutionPolicy Bypass -File ..\Build-WindowsAgentPackage.ps1 -AgentId isasha_R7_Cudy-windows
```

Check the installed task and recent agent log:

```powershell
.\Get-ManagedAgentStatus.ps1
```

When diagnosing "VPN is connected, but traffic is wrong", include network
routes, DNS/connectivity, and adapter state in the same report:

```powershell
.\Get-ManagedAgentStatus.ps1 -Network
```

Run the production smoke test after install or after changing control-server
rules. It verifies the scheduled task, SSH tunnel, control `transport_plan`,
local TUN adapter, policy route, and pinned egress probe:

```powershell
.\Test-ProdAgent.ps1
```

By default the test treats a bad provider egress as a warning after the route is
confirmed. Use strict mode when the selected provider endpoint must answer:

```powershell
.\Test-ProdAgent.ps1 -RequireProbe
```

Remove the task during rollback:

```powershell
.\Uninstall-ManagedAgentTask.ps1 -StopRunning
```

Full production rollback also stops the local control tunnel listener, stops all
managed sing-box transports, and restores direct routes:

```powershell
.\Uninstall-ManagedAgentTask.ps1 -FullRollback
```

Install the independent safety watchdog from an elevated PowerShell window:

```powershell
.\Install-AgentWatchdogTask.ps1 -RunNow `
  -CriticalService "Codex API=https://chatgpt.com/backend-api/codex/responses"
```

The watchdog runs as a separate `SYSTEM` task once per minute. It combines the
agent heartbeat with general HTTPS connectivity and the locally configured
critical-service list in `watchdog-services.json`. Three consecutive failures
create a diagnostic report, try to post it to the control-server, and run
`Emergency-Stop-Agent.ps1`. The agent task is then left disabled. A report that
cannot be posted immediately is queued under `run/` and retried after recovery.

Before a deliberately risky development command, create a one-cycle lease:

```powershell
New-Item .\run\watchdog.keepalive -ItemType File -Force
```

The watchdog consumes and deletes this file, resets its failure counter, and
skips emergency action for that cycle. `watchdog.armed` enables protection;
`watchdog.tripped.json`, `watchdog-state.json`, and `logs/watchdog.log` record
its current state and last action.

Terminal 1:

```powershell
.\Start-Tunnel.ps1
```

Terminal 2:

```powershell
.\Run-Plan.ps1
.\Apply-Routes.ps1
```

If the VPN interface is not detected automatically, pass its exact Windows
adapter name:

```powershell
.\Run-Plan.ps1 -InterfaceAlias "AmneziaVPN"
.\Apply-Routes.ps1 -InterfaceAlias "AmneziaVPN"
```

Useful checks:

```powershell
Get-NetAdapter
Get-NetRoute -AddressFamily IPv4 -DestinationPrefix 0.0.0.0/1
Get-NetRoute -AddressFamily IPv4 -DestinationPrefix 128.0.0.0/1
Get-NetRoute -AddressFamily IPv4 -DestinationPrefix 149.154.160.0/20
ping 1.1.1.1
curl.exe -4 https://ifconfig.me/ip
```

Automated smoke test after `Start-ManagedAgent.ps1 -Once`:

```powershell
.\Test-ManagedRouting.ps1
```

For legacy Cudy mode:

```powershell
.\Test-ManagedRouting.ps1 -VpnInterfaceAlias "AmneziaVPN"
```
