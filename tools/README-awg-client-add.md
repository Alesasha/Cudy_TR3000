# AWG client add utility

`awg_client_add.py` creates a separate AmneziaWG peer on an existing Docker-based AmneziaWG server and writes a client `.conf`.

Known server names:

- `cudy-home` -> `195.170.35.108:51830`, SSH through `192.168.8.1`
- `hostvds-uswest` -> `95.182.91.203:30184`
- `megahost-aktau` -> `45.136.59.135:45646`

List configured servers:

```powershell
python tools\awg_client_add.py ignored --list-servers
```

## Passwords

For one command, pass the SSH password explicitly:

```powershell
python tools\awg_client_add.py hostvds-uswest phone-alex --ssh-password '<root ssh password>'
```

Or set environment variables in PowerShell:

```powershell
$env:AWG_SSH_PASSWORD_HOSTVDS_USWEST = '<hostvds root ssh password>'
$env:AWG_SSH_PASSWORD_MEGAHOST_AKTAU = '<megahost root ssh password>'
$env:AWG_SSH_PASSWORD_CUDY_HOME = '<cudy root ssh password>'
```

If all servers use the same password, a common variable is enough:

```powershell
$env:AWG_SSH_PASSWORD = '<root ssh password>'
```

## Create Client Config

HostVDS:

```powershell
python tools\awg_client_add.py hostvds-uswest phone-alex
```

Megahost:

```powershell
python tools\awg_client_add.py megahost-aktau phone-alex
```

Cudy inbound VPN for an external user:

```powershell
python tools\awg_client_add.py cudy-home phone-alex
```

Output examples:

```text
C:\Users\Alexander\Cudy_TR3000\secrets\clients\hostvds-uswest\phone-alex.conf
C:\Users\Alexander\Cudy_TR3000\secrets\clients\megahost-aktau\phone-alex.conf
C:\Users\Alexander\Cudy_TR3000\secrets\clients\cudy-home\phone-alex-awg.conf
```

Dry run without changing the server:

```powershell
python tools\awg_client_add.py hostvds-uswest phone-alex --dry-run
python tools\awg_client_add.py megahost-aktau phone-alex --dry-run
python tools\awg_client_add.py cudy-home phone-alex --dry-run
```

Use an explicit client address:

```powershell
python tools\awg_client_add.py hostvds-uswest phone-alex --address 10.8.1.12/32
```

Linux-friendly DNS/MTU override:

```powershell
python tools\awg_client_add.py hostvds-uswest phone-alex --dns "1.1.1.1, 8.8.8.8" --mtu 1180
python tools\awg_client_add.py megahost-aktau phone-alex --dns "1.1.1.1, 8.8.8.8" --mtu 1180
python tools\awg_client_add.py cudy-home phone-alex --dns "1.1.1.1, 8.8.8.8" --mtu 1180
```

## Statistics

Show live statistics for one server:

```powershell
python tools\awg_client_add.py cudy-home --stats
python tools\awg_client_add.py hostvds-uswest --stats
python tools\awg_client_add.py megahost-aktau --stats
```

Show live statistics for every configured server:

```powershell
python tools\awg_client_add.py all --stats
```

The statistics table includes server name, endpoint, client name, allowed IPs, peer endpoint, latest handshake, received bytes, sent bytes, and keepalive.

For `cudy-home`, `from_peer_bytes` means bytes received by Cudy from the external client, and `to_peer_bytes` means bytes sent by Cudy to that client.

## What The Utility Changes

When creating a client, the utility:

1. reads `/opt/amnezia/awg/awg0.conf` inside the Docker container;
2. finds the next free `10.8.1.x/32` address;
3. generates client private/public keys and PSK inside the container;
4. appends the peer to `/opt/amnezia/awg/awg0.conf`;
5. applies the peer live with `awg set`;
6. updates `/opt/amnezia/awg/clientsTable`;
7. commits the current container back to its image tag;
8. writes the client `.conf` locally.

For `cudy-home`, the utility calls the existing `/usr/bin/friendctl` on Cudy. That command creates the peer in OpenWrt UCI, applies it live on `awg_in`, and returns the generated client config. The local copy is written to `secrets\clients\cudy-home\NAME-awg.conf`.

The script creates plain AmneziaWG/WireGuard-style `.conf` files. It does not create native Amnezia `vpn://` links.
