# route_agent.py

`route_agent.py` is the first managed route-agent prototype.

It is safe by default: `plan` does not change local routes. It fetches desired
state from the control server, resolves domains, checks current local routes,
and prints the commands that `apply` would use. Apply mode is supported on
Linux and Windows.

## Create A Device Token

On the control-server side:

```powershell
python tools\vpn_control_app.py device-create DC_via_Cudy --platform linux --display-name "Dima Linux"
```

Copy the one-time token to the client device.

## Run A Dry-Run Plan

On Linux:

```bash
export VPN_CONTROL_URL="https://control.example.net"
export VPN_AGENT_TOKEN="vca_..."
python3 tools/route_agent.py plan
```

For local testing:

```bash
export VPN_CONTROL_URL="http://127.0.0.1:8765"
export VPN_AGENT_TOKEN="vca_..."
python3 tools/route_agent.py plan
```

When a server id is known to map to a local VPN interface, pass it explicitly:

```bash
python3 tools/route_agent.py plan --interface-map cudy=amn0 --interface-map uswest=amn-uswest
```

## Apply Routes

Apply mode requires explicit confirmation and administrator/root privileges.

On Linux:

```bash
python3 tools/route_agent.py apply --direct-baseline --interface-map aktau=amn0 --yes --post-status
```

On Windows, use the helper package in `tools/agent-windows` or call the agent
directly from an elevated PowerShell:

```powershell
python .\route_agent.py apply --direct-baseline --interface-map "aktau=AmneziaVPN" --yes --post-status
```

It only executes generated route commands. It refuses to run if any configured
server id is not mapped to a local interface.

`--direct-baseline` is important when Amnezia installs full-tunnel routes
(`0.0.0.0/1` and `128.0.0.0/1`). It restores non-matched IPv4 traffic to the
physical default gateway, while selected domains/IP ranges remain on the VPN
interface.

The first output shows:

- the authenticated user and device;
- the local default route;
- detected VPN-like interfaces;
- resolved IPs for each configured domain;
- current `ip route get` result;
- dry-run route commands.

## Post Status

```bash
python3 tools/route_agent.py status
```

Or post status after building a plan:

```bash
python3 tools/route_agent.py plan --post-status
```

## Cache

Fetched config is cached by default:

```text
data/route_agent_cache.json
```

Use the cache if the control server is temporarily unavailable:

```bash
python3 tools/route_agent.py plan --cached
```

## Current Limits

- `server_id -> local interface` mapping is manual.
- Android is not supported by this helper; Android likely needs a first-party
  VPN app based on `VpnService`.
