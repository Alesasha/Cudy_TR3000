param(
    [string]$StatePath = "C:\ProgramData\CudyVPN\openai-maintenance\state.json"
)

$ErrorActionPreference = "Stop"

function Get-OpenAIIPv4([string[]]$Domains) {
    $names = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($domain in $Domains) {
        if ($domain) { [void]$names.Add($domain.Trim().TrimEnd(".")) }
    }
    Get-DnsClientCache -ErrorAction SilentlyContinue | ForEach-Object {
        $name = [string]$_.Entry
        foreach ($domain in $Domains) {
            if ($name -eq $domain -or $name.EndsWith(".$domain", [StringComparison]::OrdinalIgnoreCase)) {
                [void]$names.Add($name.TrimEnd("."))
                break
            }
        }
    }

    $addresses = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($name in $names) {
        foreach ($dnsServer in @($null, "192.168.8.1", "1.1.1.1")) {
            $resolveArgs = @{ Name=$name; Type="A"; DnsOnly=$true; ErrorAction="SilentlyContinue" }
            if ($dnsServer) { $resolveArgs.Server = $dnsServer }
            Resolve-DnsName @resolveArgs |
                Where-Object { $_.IPAddress -match '^\d+\.\d+\.\d+\.\d+$' } |
                ForEach-Object { [void]$addresses.Add([string]$_.IPAddress) }
        }
    }
    return @($addresses | Sort-Object)
}

function Get-EndpointIPv4([string]$ConfigPath) {
    $configText = Get-Content -LiteralPath $ConfigPath -Raw
    $match = [regex]::Match($configText, '(?im)^\s*Endpoint\s*=\s*([^:\s]+):\d+\s*$')
    if (-not $match.Success) { throw "The AWG config has no IPv4-compatible Endpoint entry." }
    $hostName = $match.Groups[1].Value
    $parsed = $null
    if ([System.Net.IPAddress]::TryParse($hostName, [ref]$parsed) -and
        $parsed.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) {
        return $parsed.IPAddressToString
    }
    $resolved = [System.Net.Dns]::GetHostAddresses($hostName) |
        Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork } |
        Select-Object -First 1
    if (-not $resolved) { throw "Could not resolve AWG endpoint host: $hostName" }
    return $resolved.IPAddressToString
}

function Get-PhysicalEndpointEgress([string]$EndpointPrefix, [string]$TunnelName) {
    $existing = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $EndpointPrefix -ErrorAction SilentlyContinue |
        Where-Object { $_.NextHop -and $_.NextHop -ne "0.0.0.0" } |
        Sort-Object RouteMetric, InterfaceMetric |
        Select-Object -First 1
    if ($existing) { return $existing }
    $default = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.NextHop -and $_.NextHop -ne "0.0.0.0" -and
            $_.InterfaceAlias -notin @("AmneziaVPN", $TunnelName) -and
            $_.InterfaceAlias -notmatch '^(OpenAI-|proxy|lokvpn-)'
        } |
        Sort-Object @{Expression={ $_.RouteMetric + $_.InterfaceMetric }} |
        Select-Object -First 1
    if (-not $default) { throw "No physical IPv4 route is available for $EndpointPrefix." }
    return $default
}

if (-not (Test-Path -LiteralPath $StatePath)) { exit 0 }
$state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
$endpointPrefix = [string]$state.endpoint_prefix
if (-not $endpointPrefix -or -not $state.endpoint_interface_index -or -not $state.endpoint_next_hop) {
    $endpointAddress = Get-EndpointIPv4 -ConfigPath ([string]$state.config_path)
    $endpointPrefix = "$endpointAddress/32"
    $egress = Get-PhysicalEndpointEgress -EndpointPrefix $endpointPrefix -TunnelName ([string]$state.tunnel_name)
    $state | Add-Member -NotePropertyName endpoint_prefix -NotePropertyValue $endpointPrefix -Force
    $state | Add-Member -NotePropertyName endpoint_interface -NotePropertyValue ([string]$egress.InterfaceAlias) -Force
    $state | Add-Member -NotePropertyName endpoint_interface_index -NotePropertyValue ([int]$egress.InterfaceIndex) -Force
    $state | Add-Member -NotePropertyName endpoint_next_hop -NotePropertyValue ([string]$egress.NextHop) -Force
    $state | Add-Member -NotePropertyName endpoint_route_owned -NotePropertyValue $false -Force
}
$physicalAdapter = Get-NetAdapter -Name ([string]$state.endpoint_interface) -ErrorAction SilentlyContinue
if ((-not $physicalAdapter -or $physicalAdapter.Status -ne "Up") -and $state.endpoint_wifi_profile) {
    $wlanArgs = @(
        "wlan",
        "connect",
        "name=$([string]$state.endpoint_wifi_profile)",
        "interface=$([string]$state.endpoint_interface)"
    )
    & netsh.exe @wlanArgs | Out-Null
    $wifiDeadline = [datetime]::UtcNow.AddSeconds(25)
    do {
        Start-Sleep -Milliseconds 500
        $physicalAdapter = Get-NetAdapter -Name ([string]$state.endpoint_interface) -ErrorAction SilentlyContinue
    } while ((-not $physicalAdapter -or $physicalAdapter.Status -ne "Up") -and
             [datetime]::UtcNow -lt $wifiDeadline)
}
if (-not $physicalAdapter -or $physicalAdapter.Status -ne "Up") {
    throw "Endpoint interface is not up: $($state.endpoint_interface)"
}
$oldEndpointIndex = [int]$state.endpoint_interface_index
$currentEndpointIndex = [int]$physicalAdapter.ifIndex
if ($state.endpoint_route_owned -and $oldEndpointIndex -ne $currentEndpointIndex) {
    Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $endpointPrefix `
        -InterfaceIndex $oldEndpointIndex -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
        Where-Object { $_.NextHop -eq [string]$state.endpoint_next_hop } |
        Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
}
$state.endpoint_interface_index = $currentEndpointIndex
$endpointRoute = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $endpointPrefix `
    -InterfaceIndex $currentEndpointIndex -ErrorAction SilentlyContinue |
    Where-Object { $_.NextHop -eq [string]$state.endpoint_next_hop } |
    Select-Object -First 1
if (-not $endpointRoute) {
    New-NetRoute -AddressFamily IPv4 -DestinationPrefix $endpointPrefix `
        -InterfaceIndex ([int]$state.endpoint_interface_index) `
        -NextHop ([string]$state.endpoint_next_hop) -RouteMetric 1 `
        -PolicyStore ActiveStore | Out-Null
    $state | Add-Member -NotePropertyName endpoint_route_owned -NotePropertyValue $true -Force
}
$appDefault = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" `
    -InterfaceAlias "AmneziaVPN" -ErrorAction SilentlyContinue
if ($appDefault) {
    foreach ($prefix in @($state.routes)) {
        Get-NetRoute -AddressFamily IPv4 -DestinationPrefix ([string]$prefix) `
            -InterfaceAlias ([string]$state.adapter_name) -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
            Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    }
    Stop-Service -Name ([string]$state.service_name) -Force -ErrorAction SilentlyContinue
    $state | Add-Member -NotePropertyName suspended_by_app -NotePropertyValue $true -Force
    $state.updated_at = [datetimeoffset]::UtcNow.ToString("o")
    $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StatePath -Encoding UTF8
    Write-Host "OpenAI AWG suspended while the AmneziaVPN application tunnel is active"
    exit 0
}

if ($state.suspended_by_app) {
    Start-Service -Name ([string]$state.service_name) -ErrorAction Stop
    $resumeDeadline = [datetime]::UtcNow.AddSeconds(30)
    do {
        $adapter = Get-NetAdapter -Name ([string]$state.adapter_name) -ErrorAction SilentlyContinue
        if ($adapter -and $adapter.Status -eq "Up") { break }
        Start-Sleep -Milliseconds 500
    } while ([datetime]::UtcNow -lt $resumeDeadline)
    $state | Add-Member -NotePropertyName suspended_by_app -NotePropertyValue $false -Force
}

$adapter = Get-NetAdapter -Name ([string]$state.adapter_name) -ErrorAction SilentlyContinue
if (-not $adapter -or $adapter.Status -ne "Up") {
    foreach ($prefix in @($state.routes)) {
        Get-NetRoute -AddressFamily IPv4 -DestinationPrefix ([string]$prefix) `
            -InterfaceAlias ([string]$state.adapter_name) -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
            Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    }
    throw "OpenAI AWG adapter is not up: $($state.adapter_name)"
}

$addresses = @(Get-OpenAIIPv4 -Domains @($state.domains))
if ($addresses.Count -eq 0) {
    throw "OpenAI route refresh resolved no IPv4 addresses; keeping existing routes"
}
$wanted = @($addresses | ForEach-Object { "$_/32" } | Sort-Object -Unique)
$old = @($state.routes | ForEach-Object { [string]$_ })

foreach ($prefix in $old | Where-Object { $_ -notin $wanted }) {
    Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix `
        -InterfaceAlias $adapter.Name -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
        Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
}
foreach ($prefix in $wanted) {
    $existing = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix `
        -InterfaceAlias $adapter.Name -PolicyStore ActiveStore -ErrorAction SilentlyContinue
    if (-not $existing) {
        New-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix `
            -InterfaceIndex $adapter.ifIndex -NextHop "0.0.0.0" -RouteMetric 1 `
            -PolicyStore ActiveStore | Out-Null
    }
}

$probeOutput = (& curl.exe -4 -fsS --connect-timeout 5 --max-time 15 `
    "https://chatgpt.com/cdn-cgi/trace" 2>$null | Out-String)
if ($LASTEXITCODE -ne 0 -or $probeOutput -notmatch '(?m)^ip=') {
    foreach ($prefix in $wanted) {
        Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix `
            -InterfaceAlias $adapter.Name -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
            Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    }
    $state | Add-Member -NotePropertyName degraded -NotePropertyValue $true -Force
    $state.updated_at = [datetimeoffset]::UtcNow.ToString("o")
    $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StatePath -Encoding UTF8
    throw "OpenAI AWG probe failed; dedicated routes were removed for fail-open fallback"
}

$state.routes = $wanted
$state | Add-Member -NotePropertyName degraded -NotePropertyValue $false -Force
$state.updated_at = [datetimeoffset]::UtcNow.ToString("o")
$state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StatePath -Encoding UTF8
Write-Host "OpenAI routes refreshed: $($wanted.Count) via $($adapter.Name)"
