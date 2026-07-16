param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [string]$TunnelName = "OpenAI-USWest",
    [string]$StandaloneExe = "C:\Program Files\AmneziaWG\amneziawg.exe",
    [string]$EndpointInterfaceAlias = "",
    [string]$EndpointNextHop = "",
    [string]$EndpointWiFiProfile = "",
    [string[]]$Domains = @(
        "chatgpt.com",
    "chat.openai.com",
    "ab.chatgpt.com",
    "openai.com",
    "api.openai.com",
    "auth.openai.com",
    "auth0.openai.com",
    "platform.openai.com",
    "cdn.openai.com",
    "cdn.oaistatic.com",
    "oaistatic.com",
    "oaiusercontent.com",
    "files.oaiusercontent.com"
    )
)

$ErrorActionPreference = "Stop"
$stateDir = Join-Path $env:ProgramData "CudyVPN\openai-maintenance"
$statePath = Join-Path $stateDir "state.json"
$errorPath = Join-Path $stateDir "start-error.log"
$endpointRouteCreated = $false
$endpointPrefix = ""
$endpointEgress = $null
$managedRoutes = @()
$managedAdapterName = ""
$serviceInstalled = $false
$runtimeConfig = ""
$stateWritten = $false
$oldStateInvalidated = $false

trap {
    foreach ($prefix in $managedRoutes) {
        Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix `
            -InterfaceAlias $managedAdapterName -PolicyStore ActiveStore `
            -ErrorAction SilentlyContinue |
            Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    }
    if ($serviceInstalled -and (Test-Path -LiteralPath $StandaloneExe)) {
        & $StandaloneExe /uninstalltunnelservice $TunnelName 2>$null
    }
    if ($runtimeConfig) {
        Remove-Item -LiteralPath $runtimeConfig -Force -ErrorAction SilentlyContinue
    }
    if ($stateWritten -or $oldStateInvalidated) {
        Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    }
    if ($endpointRouteCreated -and $endpointPrefix -and $endpointEgress) {
        Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $endpointPrefix `
            -InterfaceIndex $endpointEgress.InterfaceIndex -PolicyStore ActiveStore `
            -ErrorAction SilentlyContinue |
            Where-Object { $_.NextHop -eq $endpointEgress.NextHop } |
            Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Path $stateDir -Force -ErrorAction SilentlyContinue | Out-Null
    ($_ | Format-List * -Force | Out-String) | Set-Content -LiteralPath $errorPath -Encoding UTF8
    Write-Error $_
    exit 1
}

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Start-OpenAIMaintenanceTunnel.ps1 must be run as Administrator."
    }
}

function Get-OpenAIIPv4 {
    $names = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($domain in $Domains) {
        [void]$names.Add($domain.Trim().TrimEnd("."))
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
    $dnsServers = @($null, "192.168.8.1", "1.1.1.1")
    foreach ($name in $names) {
        foreach ($dnsServer in $dnsServers) {
            $resolveArgs = @{
                Name = $name
                Type = "A"
                DnsOnly = $true
                ErrorAction = "SilentlyContinue"
            }
            if ($dnsServer) { $resolveArgs.Server = $dnsServer }
            Resolve-DnsName @resolveArgs |
                Where-Object { $_.IPAddress -match '^\d+\.\d+\.\d+\.\d+$' } |
                ForEach-Object { [void]$addresses.Add([string]$_.IPAddress) }
        }
        try {
            [System.Net.Dns]::GetHostAddresses($name) |
                Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork } |
                ForEach-Object { [void]$addresses.Add($_.IPAddressToString) }
        } catch {}
    }
    return @($addresses | Sort-Object)
}

function Get-EndpointIPv4([string]$ConfigText) {
    $endpointMatch = [regex]::Match($ConfigText, '(?im)^\s*Endpoint\s*=\s*([^:\s]+):\d+\s*$')
    if (-not $endpointMatch.Success) {
        throw "The AWG config has no IPv4-compatible Endpoint entry."
    }
    $hostName = $endpointMatch.Groups[1].Value
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

function Get-EndpointEgress([string]$EndpointAddress) {
    if (($EndpointInterfaceAlias -and -not $EndpointNextHop) -or
        ($EndpointNextHop -and -not $EndpointInterfaceAlias)) {
        throw "EndpointInterfaceAlias and EndpointNextHop must be supplied together."
    }
    if ($EndpointInterfaceAlias) {
        $adapter = Get-NetAdapter -Name $EndpointInterfaceAlias -ErrorAction Stop
        if ($adapter.Status -ne "Up") { throw "Endpoint interface is not up: $EndpointInterfaceAlias" }
        return [pscustomobject]@{ InterfaceAlias=$adapter.Name; InterfaceIndex=$adapter.ifIndex; NextHop=$EndpointNextHop }
    }

    $prefix = "$EndpointAddress/32"
    $existing = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix -ErrorAction SilentlyContinue |
        Where-Object { $_.NextHop -and $_.NextHop -ne "0.0.0.0" } |
        Sort-Object RouteMetric, InterfaceMetric |
        Select-Object -First 1
    if ($existing) {
        return [pscustomobject]@{
            InterfaceAlias=[string]$existing.InterfaceAlias
            InterfaceIndex=[int]$existing.InterfaceIndex
            NextHop=[string]$existing.NextHop
        }
    }

    $excluded = @("AmneziaVPN", $TunnelName)
    $default = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.NextHop -and $_.NextHop -ne "0.0.0.0" -and
            $_.InterfaceAlias -notin $excluded -and
            $_.InterfaceAlias -notmatch '^(OpenAI-|proxy|lokvpn-)'
        } |
        Sort-Object @{Expression={ $_.RouteMetric + $_.InterfaceMetric }} |
        Select-Object -First 1
    if (-not $default) { throw "No physical IPv4 default route is available for endpoint $EndpointAddress." }
    return [pscustomobject]@{
        InterfaceAlias=[string]$default.InterfaceAlias
        InterfaceIndex=[int]$default.InterfaceIndex
        NextHop=[string]$default.NextHop
    }
}

Assert-Administrator
if (-not (Test-Path -LiteralPath $StandaloneExe)) {
    throw "Standalone AmneziaWG executable is required: $StandaloneExe"
}
$appTunnelServiceName = 'AmneziaWGTunnel$AmneziaVPN'
Get-Process -Name "AmneziaVPN" -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
if (Get-Service -Name $appTunnelServiceName -ErrorAction SilentlyContinue) {
    Stop-Service -Name $appTunnelServiceName -Force -ErrorAction SilentlyContinue
    & sc.exe delete $appTunnelServiceName | Out-Null
    $appDeadline = [datetime]::UtcNow.AddSeconds(15)
    while ((Get-NetAdapter -Name "AmneziaVPN" -ErrorAction SilentlyContinue) -and
           [datetime]::UtcNow -lt $appDeadline) {
        Start-Sleep -Milliseconds 500
    }
}
$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$configText = Get-Content -LiteralPath $resolvedConfig -Raw
$addressMatch = [regex]::Match($configText, '(?im)^\s*Address\s*=\s*([^/\s]+)')
if (-not $addressMatch.Success) {
    throw "The AWG config has no IPv4 Address entry: $resolvedConfig"
}
$transportAddress = $addressMatch.Groups[1].Value
$previous = $null
if (Test-Path -LiteralPath $statePath) {
    try {
        $previous = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    } catch {}
}
$endpointAddress = Get-EndpointIPv4 -ConfigText $configText
$endpointPrefix = "$endpointAddress/32"
$endpointEgress = Get-EndpointEgress -EndpointAddress $endpointAddress
$endpointRoute = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $endpointPrefix `
    -InterfaceIndex $endpointEgress.InterfaceIndex -ErrorAction SilentlyContinue |
    Where-Object { $_.NextHop -eq $endpointEgress.NextHop } |
    Select-Object -First 1
$endpointRouteOwned = [bool](
    $previous.endpoint_route_owned -and
    [string]$previous.endpoint_prefix -eq $endpointPrefix -and
    [int]$previous.endpoint_interface_index -eq [int]$endpointEgress.InterfaceIndex -and
    [string]$previous.endpoint_next_hop -eq [string]$endpointEgress.NextHop
)
$endpointWiFiProfile = $EndpointWiFiProfile
$endpointPhysicalAdapter = Get-NetAdapter -InterfaceIndex $endpointEgress.InterfaceIndex -ErrorAction SilentlyContinue
if (-not $endpointWiFiProfile -and
    $endpointPhysicalAdapter.InterfaceDescription -match 'Wi-?Fi|Wireless') {
    $wlanText = (& netsh.exe wlan show interfaces 2>$null | Out-String)
    $profileMatch = [regex]::Match($wlanText, '(?im)^\s*Profile\s*:\s*(.+?)\s*$')
    if ($profileMatch.Success) { $endpointWiFiProfile = $profileMatch.Groups[1].Value.Trim() }
}
if (-not $endpointRoute) {
    New-NetRoute -AddressFamily IPv4 -DestinationPrefix $endpointPrefix `
        -InterfaceIndex $endpointEgress.InterfaceIndex -NextHop $endpointEgress.NextHop `
        -RouteMetric 1 -PolicyStore ActiveStore | Out-Null
    $endpointRouteCreated = $true
    $endpointRouteOwned = $true
}
Write-Host "AWG endpoint pinned: $endpointPrefix via $($endpointEgress.InterfaceAlias) $($endpointEgress.NextHop)"
$addresses = @(Get-OpenAIIPv4)
Write-Host "OpenAI DNS bootstrap: domains=$($Domains.Count) addresses=$($addresses.Count)"
if ($addresses.Count -eq 0) {
    throw "No OpenAI IPv4 addresses could be resolved."
}

New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
Remove-Item -LiteralPath $errorPath -Force -ErrorAction SilentlyContinue

$backend = "amneziawg-standalone"
$runtimeConfig = Join-Path $stateDir "$TunnelName.conf"
$runtimeText = $configText -replace '(?im)^\s*DNS\s*=.*(?:\r?\n|$)', ''
if ($runtimeText -match '(?im)^\s*Table\s*=') {
    $runtimeText = $runtimeText -replace '(?im)^\s*Table\s*=.*$', 'Table = off'
} else {
    $runtimeText = $runtimeText -replace '(?im)^\s*\[Interface\]\s*$', "[Interface]`r`nTable = off"
}
Set-Content -LiteralPath $runtimeConfig -Value $runtimeText -Encoding ASCII

$acl = [Security.AccessControl.FileSecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
foreach ($sidValue in @('S-1-5-18', 'S-1-5-32-544')) {
    $sid = [Security.Principal.SecurityIdentifier]::new($sidValue)
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $sid,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $runtimeConfig -AclObject $acl

$serviceName = "AmneziaWGTunnel`$$TunnelName"
& $StandaloneExe /uninstalltunnelservice $TunnelName 2>$null
$oldStateInvalidated = $true
$deleteDeadline = [datetime]::UtcNow.AddSeconds(15)
while ((Get-Service -Name $serviceName -ErrorAction SilentlyContinue) -and
       [datetime]::UtcNow -lt $deleteDeadline) {
    Start-Sleep -Milliseconds 500
}

$installed = $false
for ($attempt = 1; $attempt -le 3 -and -not $installed; $attempt++) {
    & $StandaloneExe /installtunnelservice $runtimeConfig
    $serviceDeadline = [datetime]::UtcNow.AddSeconds(20)
    do {
        $tunnelService = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        if ($tunnelService -and $tunnelService.Status -eq "Running") {
            $installed = $true
            break
        }
        Start-Sleep -Milliseconds 500
    } while ([datetime]::UtcNow -lt $serviceDeadline)
    if (-not $installed -and $attempt -lt 3) {
        & $StandaloneExe /uninstalltunnelservice $TunnelName 2>$null
        Start-Sleep -Seconds 2
    }
}
if (-not $installed) {
    throw "Standalone AmneziaWG tunnel service did not reach Running state after 3 attempts."
}
$serviceInstalled = $true

$adapter = $null
$deadline = [datetime]::UtcNow.AddSeconds(30)
do {
    $transportIp = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $transportAddress -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($transportIp) {
        $adapter = Get-NetAdapter -InterfaceIndex $transportIp.InterfaceIndex -ErrorAction SilentlyContinue
    }
    if (-not $adapter) { Start-Sleep -Milliseconds 500 }
} while (-not $adapter -and [datetime]::UtcNow -lt $deadline)
if (-not $adapter) {
    throw "Could not find the AWG adapter carrying $transportAddress."
}
if ($adapter.Status -ne "Up") {
    throw "Tunnel adapter '$($adapter.Name)' is not Up."
}
$managedAdapterName = [string]$adapter.Name

foreach ($prefix in @($previous.routes)) {
    Get-NetRoute -AddressFamily IPv4 -DestinationPrefix ([string]$prefix) `
        -InterfaceAlias ([string]$previous.adapter_name) -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
        Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
}

$routes = @()
foreach ($address in $addresses) {
    $prefix = "$address/32"
    Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix `
        -InterfaceAlias $TunnelName -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
        Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    New-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix `
        -InterfaceIndex $adapter.ifIndex -NextHop "0.0.0.0" -RouteMetric 1 `
        -PolicyStore ActiveStore | Out-Null
    $routes += $prefix
    $managedRoutes += $prefix
}

$probeOutput = (& curl.exe -4 -fsS --connect-timeout 10 --max-time 25 `
    "https://chatgpt.com/cdn-cgi/trace" 2>$null | Out-String)
if ($LASTEXITCODE -ne 0 -or $probeOutput -notmatch '(?m)^ip=') {
    foreach ($prefix in $routes) {
        Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix `
            -InterfaceAlias $adapter.Name -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
            Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    }
    & $StandaloneExe /uninstalltunnelservice $TunnelName 2>$null
    throw "OpenAI AWG connectivity probe failed; explicit routes were rolled back."
}

$state = [ordered]@{
    updated_at = [datetimeoffset]::UtcNow.ToString("o")
    tunnel_name = $TunnelName
    adapter_name = [string]$adapter.Name
    adapter_address = $transportAddress
    service_name = "AmneziaWGTunnel`$$TunnelName"
    backend = $backend
    standalone_exe = $StandaloneExe
    runtime_config = $runtimeConfig
    config_path = $resolvedConfig
    endpoint_prefix = $endpointPrefix
    endpoint_interface = [string]$endpointEgress.InterfaceAlias
    endpoint_interface_index = [int]$endpointEgress.InterfaceIndex
    endpoint_next_hop = [string]$endpointEgress.NextHop
    endpoint_route_owned = $endpointRouteOwned
    endpoint_wifi_profile = $endpointWiFiProfile
    domains = $Domains
    routes = $routes
}
$state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8
$stateWritten = $true

$refreshScript = Join-Path $PSScriptRoot "Update-OpenAIMaintenanceRoutes.ps1"
if (Test-Path -LiteralPath $refreshScript) {
    $installedRefreshScript = Join-Path $stateDir "Update-OpenAIMaintenanceRoutes.ps1"
    Copy-Item -LiteralPath $refreshScript -Destination $installedRefreshScript -Force
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
        "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$installedRefreshScript`""
    )
    $startup = New-ScheduledTaskTrigger -AtStartup
    $periodic = New-ScheduledTaskTrigger -Once -At ([datetime]::Now.AddMinutes(1)) `
        -RepetitionInterval (New-TimeSpan -Minutes 2) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -StartWhenAvailable
    Register-ScheduledTask -TaskName "Cudy OpenAI Route Refresh" `
        -Action $action -Trigger @($startup, $periodic) -Settings $settings `
        -User "SYSTEM" -RunLevel Highest -Force | Out-Null
}

Write-Host "OpenAI maintenance tunnel is UP: $TunnelName via $($adapter.Name)"
Write-Host "Explicit OpenAI routes: $($routes.Count)"
Get-NetRoute -AddressFamily IPv4 -InterfaceAlias $adapter.Name -ErrorAction SilentlyContinue |
    Where-Object { $_.DestinationPrefix -in $routes } |
    Sort-Object DestinationPrefix |
    Format-Table DestinationPrefix, InterfaceAlias, NextHop, RouteMetric -AutoSize
