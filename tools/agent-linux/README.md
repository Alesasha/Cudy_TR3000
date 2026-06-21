# Linux Route Agent

This package is the Linux counterpart of the Windows managed agent.

It keeps an SSH tunnel to the control server, fetches `transport_plan`, starts
needed `sing-box` TUN exits, applies policy routes through `route_agent.py`, and
posts status/probe results back to the control server.

## Files

- `agent.env`: device settings and token.
- `managed_agent.sh`: long-running agent loop.
- `start_tunnel.sh`: pins the control-server route and opens SSH forwarding.
- `start_singbox_transport.sh`: starts one `sing-box` TUN by config.
- `stop_singbox_transport.sh`: stops one managed `sing-box` TUN by name.
- `restore_direct.sh`: restores direct half-default routes and stops managed
  provider TUN exits.
- `write_transport_plan.py`: converts control-server `transport_plan` into local `sing-box` configs.
- `install_systemd.sh`: installs a systemd service.
- `one_click_install.sh`: restores direct baseline, runs a one-shot smoke, and
  installs the systemd service.
- `status.sh`: prints service, control tunnel, route, DNS/connectivity,
  firewall/VPN conflict hints, transport, and log status.
- `uninstall_systemd.sh`: disables the service, stops managed processes, and
  restores direct routing.
- `test_prod_agent.sh`: smoke test.

## Requirements

- `python3`
- `curl`
- `iproute2`
- `ssh`
- `sing-box` in `PATH` or `./runtime/sing-box`

The agent should run as root, or it will use `sudo` for route and TUN changes.

## One-Click Install

```bash
chmod +x *.sh
./one_click_install.sh
```

If the one-shot smoke was already done and you only want to install the service:

```bash
./one_click_install.sh --skip-smoke
```

Check status:

```bash
./status.sh
```

When debugging a remote machine, ask the user to run only this command and send
the full output. It is read-only and includes the checks that usually explain
"VPN is connected, but internet is gone": broken DNS, dead public IP
connectivity, UFW/nft/iptables drop or reject rules, Amnezia/WireGuard/sing-box
interfaces, and recent relevant system log hints.

## Manual One-Shot Test

```bash
chmod +x *.sh
RUN_ONCE=1 ./managed_agent.sh
./test_prod_agent.sh
```

If internet disappears during a test, run:

```bash
./restore_direct.sh
```

The restore script uses the first non-VPN IPv4 default route, rewrites
`0.0.0.0/1` and `128.0.0.0/1` to that gateway, stops managed provider TUN
exits, and resets `systemd-resolved` DNS on that physical interface when
`resolvectl` is available.

## Install

```bash
sudo ./install_systemd.sh
```

Check logs:

```bash
./status.sh
journalctl -u cudy-managed-agent.service -f
```

## Rollback

```bash
sudo ./uninstall_systemd.sh
```
