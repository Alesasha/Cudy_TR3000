param(
    [string]$VpnInterfaceAlias = "AmneziaVPN",
    [string]$RoutedHost = "ifconfig.me",
    [string]$RoutedIp = "34.160.111.145",
    [string]$TelegramProbeIp = "149.154.160.1",
    [string]$DirectProbeIp = "1.1.1.1",
    [string[]]$ManagedInterfaceAliases = @("AmneziaVPN", "proxyde", "proxyfr", "proxygb", "proxykz", "proxynl", "proxyru", "proxytr", "proxyus"),
    [int]$MinRoutedRxBytes = 1000
)

$ErrorActionPreference = "Stop"

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

function Get-VpnStats {
    param([string]$InterfaceAlias = $VpnInterfaceAlias)
    Get-NetAdapterStatistics -Name $InterfaceAlias -ErrorAction Stop |
        Select-Object Name, ReceivedBytes, SentBytes
}

$checks = @(
    [pscustomobject]@{ Target = $DirectProbeIp; Expected = "not:$VpnInterfaceAlias"; Route = Find-BestIPv4Route $DirectProbeIp },
    [pscustomobject]@{ Target = $RoutedIp; Expected = "managed"; Route = Find-BestIPv4Route $RoutedIp },
    [pscustomobject]@{ Target = $TelegramProbeIp; Expected = "managed"; Route = Find-BestIPv4Route $TelegramProbeIp }
)

Write-Host "== route expectations =="
$checks |
    ForEach-Object {
        $actual = if ($null -eq $_.Route) { "-" } else { $_.Route.InterfaceAlias }
        $ok = if ($_.Expected.StartsWith("not:")) {
            $actual -ne $_.Expected.Substring(4)
        } elseif ($_.Expected -eq "managed") {
            $ManagedInterfaceAliases -contains $actual
        } else {
            $actual -eq $_.Expected
        }
        [pscustomobject]@{
            Target = $_.Target
            Expected = $_.Expected
            Actual = $actual
            DestinationPrefix = if ($null -eq $_.Route) { "-" } else { $_.Route.DestinationPrefix }
            OK = $ok
        }
    } |
    Tee-Object -Variable routeResults |
    Format-Table -AutoSize

$routeFailed = $routeResults | Where-Object { -not $_.OK }
if ($routeFailed) {
    throw "Route expectation failed."
}

Write-Host "`n== direct probe =="
$beforeDirect = Get-VpnStats
curl.exe -4 --connect-timeout 10 --max-time 30 https://ifconfig.me/ip
Write-Host ""
$afterDirect = Get-VpnStats
Write-Host "vpn_delta_rx=$($afterDirect.ReceivedBytes - $beforeDirect.ReceivedBytes) vpn_delta_tx=$($afterDirect.SentBytes - $beforeDirect.SentBytes)"

Write-Host "`n== routed probe =="
$routedRoute = Find-BestIPv4Route $RoutedIp
if ($null -eq $routedRoute -or -not ($ManagedInterfaceAliases -contains $routedRoute.InterfaceAlias)) {
    throw "Routed probe target is not routed through a managed interface."
}
$routedInterface = $routedRoute.InterfaceAlias
$beforeRouted = Get-VpnStats -InterfaceAlias $routedInterface
curl.exe -4 --resolve "${RoutedHost}:443:${RoutedIp}" "https://${RoutedHost}/" -o NUL --connect-timeout 10 --max-time 30
$afterRouted = Get-VpnStats -InterfaceAlias $routedInterface
$deltaRx = $afterRouted.ReceivedBytes - $beforeRouted.ReceivedBytes
$deltaTx = $afterRouted.SentBytes - $beforeRouted.SentBytes
Write-Host "routed_interface=$routedInterface vpn_delta_rx=$deltaRx vpn_delta_tx=$deltaTx"

if ($deltaRx -lt $MinRoutedRxBytes) {
    throw "Routed probe did not move enough traffic through $routedInterface."
}

Write-Host "`nPASS: managed routing is active."
