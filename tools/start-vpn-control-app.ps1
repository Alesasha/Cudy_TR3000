$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$python = "python"
$port = 8765

$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalAddress -eq "0.0.0.0" -or $_.LocalAddress -eq "::" }

if ($listener) {
    exit 0
}

Start-Process `
    -FilePath $python `
    -ArgumentList @("tools\vpn_control_app.py", "serve", "--host", "0.0.0.0", "--port", "$port") `
    -WorkingDirectory $root `
    -WindowStyle Hidden
