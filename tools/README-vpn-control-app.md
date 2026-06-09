# vpn_control_app.py

`vpn_control_app.py` is the local SQLite-backed control panel for users, domain routes, Auto cache, Auto candidate lists, and Cudy route deployment.

It runs on the operator Windows machine, not on the Cudy router. If Windows reboots, the web panel will not start automatically unless a Windows Scheduled Task or another service wrapper starts it.

## Start The Web Panel

```powershell
cd C:\Users\Alexander\Cudy_TR3000
python tools\vpn_control_app.py serve --host 127.0.0.1 --port 8765
```

Open:

```text
http://127.0.0.1:8765/
http://127.0.0.1:8765/admin
```

Health check:

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/healthz
```

## Local Autostart

The Cudy router services can survive a Cudy reboot through OpenWrt init/UCI, but this local web panel is a Windows process. To autostart it after Windows login, create a Scheduled Task that runs:

```powershell
python C:\Users\Alexander\Cudy_TR3000\tools\vpn_control_app.py serve --host 127.0.0.1 --port 8765
```

Use `C:\Users\Alexander\Cudy_TR3000` as the working directory.

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

## Auto

Manual Auto cache:

```powershell
python tools\vpn_control_app.py auto-cache-list
python tools\vpn_control_app.py auto-cache-set example.com proxyde --score-ms 120
python tools\vpn_control_app.py auto-cache-delete example.com
```

Ordered candidate lists for future benchmark:

```powershell
python tools\vpn_control_app.py auto-candidates-list
python tools\vpn_control_app.py auto-candidates-set "proxyde, proxyus, uswest"
python tools\vpn_control_app.py auto-candidates-set "proxygb, proxyde" --domain example.com
python tools\vpn_control_app.py auto-candidates-set "proxynl, proxyde" --user-id test-client-awg --domain example.com
```
