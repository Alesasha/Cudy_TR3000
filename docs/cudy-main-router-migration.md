# Cudy Main Router Migration

This note tracks the AirTies Air4452 -> Cudy/OpenWrt migration plan.

Full raw snapshots are stored under `backups/` and are intentionally ignored by
git because they include local MAC addresses, hostnames, Wi-Fi details, and
router state:

- `backups/airties/snapshots/20260625-102516/`
- `backups/cudy/snapshots/20260625-102803/`

Capture a fresh read-only, redacted Cudy snapshot before every preflight:

```powershell
python tools\capture_cudy_preflight_snapshot.py --host 192.168.8.1
```

The snapshot records command exit codes and redacts private keys, preshared
keys, passwords and tokens. It is suitable for analysis and source control
verification, but it is not a restorable backup. A full on-router
`sysupgrade -b` archive is still mandatory immediately before cutover.

To regenerate the local dry-run migration plan from those snapshots:

```powershell
python tools\generate_cudy_router_migration.py `
  --airties-snapshot backups\airties\snapshots\20260625-102516 `
  --cudy-snapshot backups\cudy\snapshots\20260625-102803
```

The generated shell plan is guarded with an early `exit 1`. It is intended for
review and editing before any router cutover, not direct execution.

To run an offline preflight risk check:

```powershell
python tools\cudy_router_preflight.py `
  --airties-snapshot backups\airties\snapshots\20260625-102516 `
  --cudy-snapshot backups\cudy\snapshots\20260625-102803
```

The preflight writes ignored local reports:

- `backups/airties/snapshots/20260625-102516/cudy-main-router-preflight.md`
- `backups/airties/snapshots/20260625-102516/cudy-main-router-preflight.json`

## Current Topology

- AirTies is the current ISP-facing router.
- Cudy is behind AirTies on AirTies LAN and currently uses AirTies as its WAN
  gateway.
- Cudy LAN is a separate subnet used by the workstation and Cudy VPN clients.
- Cudy already carries the VPN/proxy logic and must not lose its existing
  OpenWrt firewall zones, AWG interfaces, route-agent state, and provider
  transport wiring.

## AirTies Settings To Recreate

WAN:

- Static public IPv4 with `/24` mask.
- ISP gateway on the same public `/24`.
- ISP DNS servers are explicitly configured.
- WAN is Ethernet with VLAN ID `2`.

LAN/DHCP:

- LAN subnet is `192.168.1.0/24`.
- Router LAN address is `192.168.1.1`.
- DHCP pool is `192.168.1.10` through `192.168.1.249`.
- DHCP lease time is `3600` seconds.
- AirTies has a non-trivial static DHCP reservation list. Use the detailed
  ignored snapshot before cutover.

Wi-Fi:

- One primary 2.4 GHz SSID is enabled.
- Additional SSIDs are present but disabled.
- WPS is enabled on AirTies; prefer disabling it on Cudy unless explicitly
  needed.

NAT/firewall:

- NAT and port forwarding are enabled.
- AirTies firewall is disabled.
- Remote management and telnet are enabled on AirTies.
- UPnP and TR-069 are enabled on AirTies.
- Several active port forwards exist for cameras, RDP, CZ_API, Home Assistant,
  and the Cudy AWG listener. Recreate only the forwards that are still needed.

## Cudy Cutover Rules

Do not apply these changes until the physical ISP uplink is moved to Cudy and a
rollback path through AirTies is available.

1. Back up Cudy config immediately before the change.
2. Configure Cudy WAN for ISP VLAN `2` and the AirTies static WAN values.
3. Move Cudy LAN from the current test subnet to the AirTies LAN subnet only
   when ready to make Cudy the main router.
4. Recreate DHCP reservations from the ignored AirTies snapshot.
5. Recreate only required port forwards.
6. Open the AWG listen port on Cudy WAN directly. Do not create a self-forward
   for the Cudy AWG listener after Cudy becomes the border router.
7. Do not recreate AirTies self-forwards to `192.168.1.1`; after cutover that
   address belongs to Cudy.
8. Keep Cudy VPN/proxy firewall zones and policy routing intact.
9. Disable remote telnet, remote web management, UPnP, WPS, and TR-069 unless
   there is an explicit operational reason to keep them.
10. Validate external access from a non-home network after cutover.

## Validation Checklist

- Cudy can ping the ISP gateway from WAN.
- Cudy can resolve DNS through the configured ISP DNS and through a public
  resolver.
- A LAN client receives the expected `192.168.1.0/24` DHCP lease.
- Existing local devices with reservations keep their expected addresses.
- Internet works for a normal LAN client without VPN.
- Cudy AWG external clients can connect to the public IP and AWG port.
- Cudy route-agent/VPN provider routing still works for Telegram, Gemini,
  `ifconfig.me`, and direct traffic.
- Required camera/Home Assistant/RDP/CZ_API forwards work externally.
- AirTies can be reconnected quickly if WAN VLAN/static routing fails.

## Current Preflight Notes

The fresh read-only preflight on 2026-07-18 found `FAIL=0`, `PASS=3`, `WARN=5`
and confirmed healthy AWG/firewall prerequisites. It highlighted these items to
resolve before cutover:

- AirTies WAN uses VLAN `2`; confirm the correct OpenWrt syntax for this Cudy
  build before applying any WAN change.
- Cudy currently has host routes via the old AirTies LAN gateway
  `192.168.1.1`; those routes must be reviewed after Cudy becomes the gateway.
- Some active AirTies port-forward targets are not DHCP-reserved in AirTies.
  They may be static on the devices, but verify them before relying on forwards.
- AirTies remote management, UPnP, and TR-069 were enabled. Prefer keeping
  these disabled on Cudy.
- Both Cudy Wi-Fi interfaces are currently disabled and configured without
  encryption. Configure encrypted SSIDs and test them before moving LAN clients.
