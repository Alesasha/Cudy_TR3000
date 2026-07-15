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
- `install_singbox_runtime.sh`: downloads the current Linux `sing-box`
  runtime into `./runtime/sing-box` when it is not already bundled.
- `restore_direct.sh`: restores direct half-default routes and stops managed
  provider TUN exits.
- `run_diagnostics.sh`: collects route, DNS, service, transport, firewall, and
  recent journal hints, posts the report to the control server, falls back to
  email when possible, and shows the report locally for copy/paste.
- `write_transport_plan.py`: converts control-server `transport_plan` into local `sing-box` configs.
- `install_systemd.sh`: installs a systemd service.
- `one_click_install.sh`: restores direct baseline, runs a one-shot smoke, and
  installs the systemd service.
- `status.sh`: prints service, control tunnel, route, DNS/connectivity,
  firewall/VPN conflict hints, transport, and log status.
- `uninstall_systemd.sh`: disables the service, stops managed processes, and
  restores direct routing.
- `watch_agent_connectivity.py`: independent critical-service watchdog. The
  systemd timer runs it every minute; after three consecutive failures it posts
  diagnostics, disables the managed agent, and restores direct routing.
- `test_prod_agent.sh`: smoke test.

## Requirements

- `python3`
- `curl`
- `iproute2`
- `ssh`
- `sing-box` in `PATH` or `./runtime/sing-box`; `one_click_install.sh`
  can download it automatically when `curl`, `tar`, and internet access are
  available.

The agent should run as root, or it will use `sudo` for route and TUN changes.

## One-Click Install

Build or refresh a per-device prod zip from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File tools\Build-LinuxAgentPackage.ps1 -AgentId DC_via_Cudy-linux
```

The generated archive is written to `secrets/agents/DC_via_Cudy-linux-prod.zip`.
It includes current Linux scripts, shared `route_agent.py`, `agent.env`, and the
control-tunnel SSH key from the ignored per-device secrets directory.

```bash
chmod +x *.sh
./one_click_install.sh
```

Set `AUTO_INSTALL_SINGBOX=0` if the machine must not download binaries and the
runtime is already provided by another channel.

If the one-shot smoke was already done and you only want to install the service:

```bash
./one_click_install.sh --skip-smoke
```

Check status:

```bash
./status.sh
```

Open the desktop UI:

```bash
./cudy_agent_ui.sh
```

The preferred UI uses Python/Tk and stays open after ON/OFF/Status/Diagnostics
actions. `one_click_install.sh` checks this dependency and, on apt-based
systems such as Ubuntu/Linux Mint, tries to install `python3-tk` automatically.
Set `CUDY_SKIP_UI_DEPS=1` before running the installer to skip this optional
step. If the automatic install is skipped or fails, install Tk support
explicitly:

```bash
python3 - <<'PY'
import tkinter
print("tk ok")
PY
sudo apt update && sudo apt install -y python3-tk
```

Run diagnostics from the desktop shortcut menu or directly:

```bash
./run_diagnostics.sh
```

The diagnostics report is saved under `logs/`, sent to the control server when
the control tunnel is reachable, copied to the clipboard when `wl-copy`, `xclip`,
or `xsel` is available, and displayed locally so the user can copy it manually
if automatic delivery fails.

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

The installer enables both `cudy-managed-agent.service` and
`cudy-managed-agent-watchdog.timer`. A transient watchdog failure is recorded
without marking the timer unit failed; only `--probe-only` returns a failing
exit code for diagnostics.

Check logs:

```bash
./status.sh
journalctl -u cudy-managed-agent.service -f
```

## Rollback

```bash
sudo ./uninstall_systemd.sh
```
