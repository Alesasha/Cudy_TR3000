param(
    [string]$VpnInterfaceAlias = "AmneziaVPN"
)

$ErrorActionPreference = "Continue"
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [Console]::OutputEncoding
} catch {
}

function ConvertTo-UInt32Address {
    param([string]$Address)
    $bytes = [System.Net.IPAddress]::Parse($Address).GetAddressBytes()
    [Array]::Reverse($bytes)
    return [BitConverter]::ToUInt32($bytes, 0)
}

function Test-RouteMatch {
    param(
        [uint32]$Target,
        [string]$DestinationPrefix
    )
    $parts = $DestinationPrefix -split "/"
    if ($parts.Count -ne 2) {
        return $false
    }
    $network = ConvertTo-UInt32Address $parts[0]
    $prefixLength = [int]$parts[1]
    if ($prefixLength -eq 0) {
        return $true
    }
    $mask = [uint32]::MaxValue -shl (32 - $prefixLength)
    return (($Target -band $mask) -eq ($network -band $mask))
}

function Find-BestIPv4Route {
    param([string]$Target)
    $targetInt = ConvertTo-UInt32Address $Target
    Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { Test-RouteMatch -Target $targetInt -DestinationPrefix $_.DestinationPrefix } |
        Sort-Object `
            @{ Expression = { [int](($_.DestinationPrefix -split "/")[1]) }; Descending = $true },
            RouteMetric,
            InterfaceMetric |
        Select-Object -First 1
}

Write-Host "== adapters =="
Get-NetAdapter |
    Sort-Object InterfaceIndex |
    Format-Table Name, InterfaceDescription, Status, InterfaceIndex -AutoSize

Write-Host "`n== ipv4 interface metrics =="
Get-NetIPInterface -AddressFamily IPv4 |
    Sort-Object InterfaceMetric |
    Format-Table InterfaceAlias, InterfaceIndex, ConnectionState, InterfaceMetric -AutoSize

Write-Host "`n== sensitive routes =="
$prefixes = @(
    "0.0.0.0/0",
    "0.0.0.0/1",
    "128.0.0.0/1",
    "192.168.8.0/24",
    "192.168.8.102/32",
    "95.182.91.203/32"
)
Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.DestinationPrefix -in $prefixes -or $_.InterfaceAlias -eq $VpnInterfaceAlias -or $_.InterfaceAlias -match '^(proxy|lokvpn|lok)[A-Za-z0-9_.-]*$' } |
    Sort-Object DestinationPrefix, RouteMetric, InterfaceMetric |
    Format-Table DestinationPrefix, InterfaceAlias, NextHop, RouteMetric, InterfaceMetric -AutoSize

Write-Host "`n== route resolution =="
@("1.1.1.1", "95.182.91.203", "149.154.160.1", "104.17.147.22") |
    ForEach-Object {
        $target = $_
        $route = Find-BestIPv4Route $target
        if ($null -eq $route) {
            [pscustomobject]@{
                Target = $target
                DestinationPrefix = "-"
                InterfaceAlias = "-"
                NextHop = "-"
                RouteMetric = "-"
                InterfaceMetric = "-"
            }
        } else {
            [pscustomobject]@{
                Target = $target
                DestinationPrefix = $route.DestinationPrefix
                InterfaceAlias = $route.InterfaceAlias
                NextHop = $route.NextHop
                RouteMetric = $route.RouteMetric
                InterfaceMetric = $route.InterfaceMetric
            }
        }
    } |
    Format-Table Target, DestinationPrefix, InterfaceAlias, NextHop, RouteMetric, InterfaceMetric -AutoSize

Write-Host "`n== connectivity =="
ping.exe -n 4 1.1.1.1
curl.exe -4 --connect-timeout 5 https://ifconfig.me/ip
Write-Host ""
