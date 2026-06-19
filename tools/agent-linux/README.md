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
- `write_transport_plan.py`: converts control-server `transport_plan` into local `sing-box` configs.
- `install_systemd.sh`: installs a systemd service.
- `test_prod_agent.sh`: smoke test.

## Requirements

- `python3`
- `curl`
- `iproute2`
- `ssh`
- `sing-box` in `PATH` or `./runtime/sing-box`

The agent should run as root, or it will use `sudo` for route and TUN changes.

## One-Shot Test

```bash
chmod +x *.sh
RUN_ONCE=1 ./managed_agent.sh
./test_prod_agent.sh
```

## Install

```bash
sudo ./install_systemd.sh
```

Check logs:

```bash
journalctl -u cudy-managed-agent.service -f
tail -f managed-agent.log
```

## Rollback

```bash
sudo systemctl disable --now cudy-managed-agent.service
sudo ./restore_direct.sh
```
