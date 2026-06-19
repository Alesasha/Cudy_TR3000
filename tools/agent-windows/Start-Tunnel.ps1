param(
    [string]$HostName = "95.182.91.203",
    [string]$User = "cudy-tunnel-windows",
    [string]$KeyPath = "$PSScriptRoot\uswest_control_tunnel_ed25519",
    [int]$LocalPort = 18765,
    [int]$RemotePort = 8765
)

$ErrorActionPreference = "Stop"

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-PhysicalDefaultRoute {
    $vpnPattern = "(?i)(amn|amnezia|wireguard|wintun|openvpn|tap|tun|wg)"
    $routes = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -ErrorAction Stop |
        Sort-Object RouteMetric, InterfaceMetric
    foreach ($route in $routes) {
        $adapter = Get-NetAdapter -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue
        if ($null -eq $adapter -or $adapter.Status -ne "Up") {
            continue
        }
        if ("$($adapter.Name) $($adapter.InterfaceDescription)" -notmatch $vpnPattern) {
            return $route
        }
    }
    return $routes | Select-Object -First 1
}

$existing = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "127.0.0.1:$LocalPort is already listening."
    return
}

if (Test-Path $KeyPath) {
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    icacls $KeyPath /inheritance:r | Out-Null
    icacls $KeyPath /grant:r "${currentUser}:R" | Out-Null
}

$hostIp = ([System.Net.Dns]::GetHostAddresses($HostName) | Where-Object { $_.AddressFamily -eq "InterNetwork" } | Select-Object -First 1).IPAddressToString
if (-not $hostIp) {
    throw "Cannot resolve $HostName to IPv4."
}

if (Test-Admin) {
    $route = Get-PhysicalDefaultRoute
    if ($null -ne $route) {
        $dest = "$hostIp/32"
        Remove-NetRoute -DestinationPrefix $dest -PolicyStore ActiveStore -Confirm:$false -ErrorAction SilentlyContinue
        New-NetRoute -DestinationPrefix $dest -InterfaceIndex $route.InterfaceIndex -NextHop $route.NextHop -RouteMetric 1 -PolicyStore ActiveStore | Out-Null
        Write-Host "Pinned $dest via ifIndex=$($route.InterfaceIndex) nextHop=$($route.NextHop)."
    }
} else {
    Write-Warning "Run this script as Administrator to pin $hostIp outside the VPN before opening the SSH tunnel."
}

Write-Host "Opening SSH tunnel: http://127.0.0.1:$LocalPort -> ${User}@${HostName}:127.0.0.1:$RemotePort"
Write-Host "Keep this window open while the agent is running."

ssh -i $KeyPath `
    -o ExitOnForwardFailure=yes `
    -o ConnectTimeout=60 `
    -o ServerAliveInterval=30 `
    -o ServerAliveCountMax=3 `
    -N -L "${LocalPort}:127.0.0.1:${RemotePort}" `
    "${User}@${HostName}"
