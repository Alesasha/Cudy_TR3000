param(
    [string]$PhysicalInterfaceAlias = "Ethernet",
    [string]$VpnInterfaceAlias = "AmneziaVPN",
    [string[]]$VpnInterfaceAliases = @("AmneziaVPN"),
    [string]$Gateway = "192.168.8.1",
    [switch]$StopSingBox,
    [switch]$KeepTunnel
)

$ErrorActionPreference = "Continue"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Restore-Direct.ps1 must be run as Administrator."
}

if (-not $KeepTunnel) {
    foreach ($alias in $VpnInterfaceAliases) {
        Stop-Service -Name "AmneziaWGTunnel`$$alias" -Force -ErrorAction SilentlyContinue
    }
    if ($StopSingBox -and (Test-Path -LiteralPath "$PSScriptRoot\Stop-SingBoxTransport.ps1")) {
        & "$PSScriptRoot\Stop-SingBoxTransport.ps1" -All
    }
}

if ($VpnInterfaceAlias -and $VpnInterfaceAlias -notin $VpnInterfaceAliases) {
    $VpnInterfaceAliases += $VpnInterfaceAlias
}

$physical = Get-NetAdapter -Name $PhysicalInterfaceAlias -ErrorAction Stop
Set-NetIPInterface -InterfaceIndex $physical.InterfaceIndex -AddressFamily IPv4 -InterfaceMetric 1 -ErrorAction SilentlyContinue

$prefixes = @(
    "0.0.0.0/0",
    "0.0.0.0/1",
    "128.0.0.0/1",
    "192.168.8.0/24",
    "192.168.8.102/32",
    "192.168.8.1/32",
    "192.168.8.255/32",
    "95.182.91.203/32"
)

foreach ($dest in $prefixes) {
    foreach ($alias in $VpnInterfaceAliases) {
        Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $dest -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
            Where-Object { $_.InterfaceAlias -eq $alias } |
            Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    }
}

foreach ($dest in @("0.0.0.0/1", "128.0.0.0/1")) {
    Remove-NetRoute -AddressFamily IPv4 -DestinationPrefix $dest -PolicyStore ActiveStore -Confirm:$false -ErrorAction SilentlyContinue
}

$default = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -InterfaceIndex $physical.InterfaceIndex -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
    Where-Object { $_.NextHop -eq $Gateway } |
    Select-Object -First 1
if ($null -eq $default) {
    New-NetRoute -DestinationPrefix "0.0.0.0/0" -InterfaceIndex $physical.InterfaceIndex -NextHop $Gateway -RouteMetric 0 -PolicyStore ActiveStore -ErrorAction SilentlyContinue | Out-Null
}

Set-DnsClientServerAddress -InterfaceIndex $physical.InterfaceIndex -ServerAddresses $Gateway -ErrorAction SilentlyContinue

Write-Host "Direct baseline restored via $Gateway on $PhysicalInterfaceAlias."
Write-Host ""
Get-NetRoute -AddressFamily IPv4 |
    Where-Object { $_.DestinationPrefix -in @("0.0.0.0/0", "0.0.0.0/1", "128.0.0.0/1", "95.182.91.203/32") -or $_.InterfaceAlias -in $VpnInterfaceAliases } |
    Sort-Object DestinationPrefix, RouteMetric, InterfaceMetric |
    Format-Table DestinationPrefix, InterfaceAlias, NextHop, RouteMetric, InterfaceMetric -AutoSize

Write-Host "`nConnectivity:"
ping.exe -n 2 1.1.1.1
curl.exe -4 --connect-timeout 5 https://ifconfig.me/ip
Write-Host ""
