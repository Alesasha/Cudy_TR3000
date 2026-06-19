param(
    [string]$AktauInterface = "AmneziaVPN",
    [string]$ProbeHost = "ifconfig.me",
    [string]$ProbeIp = "34.160.111.145"
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Test-ControlTransportPlan.ps1 must be run as Administrator."
    }
}

Assert-Admin

& "$PSScriptRoot\Start-ManagedAgent.ps1" `
    -NoDirectTransports `
    -ExtraInterfaceMap "aktau=$AktauInterface" `
    -Once

Write-Host ""
Write-Host "== route check =="
Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "$ProbeIp/32" -ErrorAction SilentlyContinue |
    Sort-Object RouteMetric, InterfaceMetric |
    Select-Object DestinationPrefix, InterfaceAlias, NextHop, RouteMetric, InterfaceMetric |
    Format-Table -AutoSize

Write-Host ""
Write-Host "== proxyde adapter =="
Get-NetAdapter -Name "proxyde" -ErrorAction SilentlyContinue |
    Select-Object Name, Status, InterfaceIndex, InterfaceDescription |
    Format-Table -AutoSize

Write-Host ""
Write-Host "== pinned probe =="
curl.exe -4 --resolve "${ProbeHost}:443:${ProbeIp}" "https://${ProbeHost}/ip"
