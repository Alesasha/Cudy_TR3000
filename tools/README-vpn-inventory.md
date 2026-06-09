# vpn_inventory.py

`vpn_inventory.py` manages the static VPN/proxy inventory and reads selected runtime state from Cudy.

It does not replace the provider refresh scripts already installed on Cudy. It can validate local inventory, list server choices, take a runtime snapshot, and trigger existing Cudy refresh commands over SSH.

## Basic Commands

Validate inventory:

```powershell
python tools\vpn_inventory.py validate
```

List user-visible servers:

```powershell
python tools\vpn_inventory.py list
```

List admin-visible servers, including disabled/stale entries:

```powershell
python tools\vpn_inventory.py admin-list --include-disabled
```

## Cudy Runtime Snapshot

Refresh the local runtime snapshot from Cudy:

```powershell
python tools\vpn_inventory.py refresh-cudy
```

Default output:

```text
config\cudy-runtime.json
```

The Cudy SSH password is read from `CUDY_SSH_PASSWORD` when needed.

## Provider Refresh

Preview provider refresh commands:

```powershell
python tools\vpn_inventory.py refresh-provider
python tools\vpn_inventory.py refresh-provider vpntype
python tools\vpn_inventory.py refresh-provider lokvpn --profile fr2
python tools\vpn_inventory.py refresh-provider proxyde
```

Actually run refresh commands on Cudy:

```powershell
python tools\vpn_inventory.py refresh-provider --apply
```

This calls the scripts that already live on Cudy, such as:

- `/usr/bin/vpntype-proxy-refresh-all`
- `/usr/bin/lokvpn-refresh-current`

## Source Files

Static inventory:

```text
config\vpn_inventory.json
```

Runtime snapshot:

```text
config\cudy-runtime.json
```

Runtime snapshots are ignored by git.
