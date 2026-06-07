# Inventory

## Static Catalog

The static catalog is stored in:

```text
config/vpn_inventory.json
```

It contains:

- user-visible choices, including `Auto`;
- admin-visible disabled/stale entries;
- provider, interface, geo, and switch command metadata;
- LokVPN profile metadata and 12 user-visible LokVPN choices;
- role permissions for the future admin/user UI.

User-visible list:

```powershell
python tools\vpn_inventory.py list
```

Admin-visible list:

```powershell
python tools\vpn_inventory.py admin-list --include-disabled
```

Validate the catalog:

```powershell
python tools\vpn_inventory.py validate
```

## Runtime Snapshot

Runtime state is collected from Cudy over SSH:

```powershell
$env:CUDY_SSH_PASSWORD = '<router password>'
python tools\vpn_inventory.py refresh-cudy
Remove-Item Env:CUDY_SSH_PASSWORD
```

The output file is:

```text
config/cudy-runtime.json
```

This file is ignored by Git because it is a changing local snapshot.

The snapshot records:

- PBR supported interfaces;
- current `TARGET_INTERFACE`;
- current network links and IPv4 addresses;
- sing-box service status;
- current VPNtype final tag;
- current LokVPN profile;
- presence of provider refreshers and switchers.

## Provider Refresh

Cudy remains the source of truth for provider refresh mechanics:

- VPNtype proxy endpoints are refreshed by `/usr/bin/vpntype-proxy-refresh-all`, including the existing cron job;
- individual VPNtype proxies use `/usr/bin/proxy*-refresh`;
- LokVPN profile refresh uses `/usr/bin/lokvpn-refresh-current`.

The local project wraps these commands so they can be previewed and, when needed, executed over SSH:

```powershell
python tools\vpn_inventory.py refresh-provider
python tools\vpn_inventory.py refresh-provider vpntype
python tools\vpn_inventory.py refresh-provider lokvpn --profile fr2
python tools\vpn_inventory.py refresh-provider proxyde
```

All of the commands above are dry-run previews. Add `--apply` only when the refresh should actually run on Cudy:

```powershell
$env:CUDY_SSH_PASSWORD = '<router password>'
python tools\vpn_inventory.py refresh-provider --apply
Remove-Item Env:CUDY_SSH_PASSWORD
```

## Current Stage 1 Choice Count

The intended user list is 32 choices:

- `Auto`;
- two own exits: Aktau and US West;
- 12 LokVPN profile exits;
- 17 VPNtype proxy exits.

Admin mode also sees disabled/stale entries and internal selectors.
