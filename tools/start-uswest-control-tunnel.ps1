param(
    [string]$HostName = "95.182.91.203",
    [string]$User = "root",
    [int]$LocalPort = 8765,
    [int]$RemotePort = 8765
)

$ErrorActionPreference = "Stop"

$existing = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "127.0.0.1:$LocalPort is already listening."
    return
}

Write-Host "Opening SSH tunnel: http://127.0.0.1:$LocalPort -> ${User}@${HostName}:127.0.0.1:$RemotePort"
Write-Host "Keep this window open while using the control panel."

ssh -N -L "${LocalPort}:127.0.0.1:${RemotePort}" "${User}@${HostName}"
