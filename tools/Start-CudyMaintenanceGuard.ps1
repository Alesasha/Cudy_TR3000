param(
    [string]$WifiAlias = "",
    [string]$WifiProfile = "",
    [string]$EthernetAlias = "Ethernet",
    [string]$TunnelAlias = "AmneziaVPN",
    [string]$TunnelEndpoint = "",
    [string]$CudyAddress = "192.168.8.1",
    [string]$RequiredUrl = "https://chatgpt.com/cdn-cgi/trace",
    [int]$IntervalSeconds = 10,
    [switch]$Monitor
)

$ErrorActionPreference = "Stop"
$stateDir = Join-Path $env:ProgramData "CudyVPN\maintenance-guard"
$statePath = Join-Path $stateDir "state.json"
$pidPath = Join-Path $stateDir "monitor.pid"
$logPath = Join-Path $stateDir "guard.log"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Quote-Argument([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Test-IsPublicIPv4([string]$Address) {
    $parsed = [Net.IPAddress]::None
    if (-not [Net.IPAddress]::TryParse($Address, [ref]$parsed)) { return $false }
    $bytes = $parsed.GetAddressBytes()
    if ($bytes.Length -ne 4) { return $false }
    if ($bytes[0] -in @(0, 10, 127)) { return $false }
    if ($bytes[0] -eq 169 -and $bytes[1] -eq 254) { return $false }
    if ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) { return $false }
    if ($bytes[0] -eq 192 -and $bytes[1] -eq 168) { return $false }
    if ($bytes[0] -ge 224) { return $false }
    return $true
}

function Write-GuardLog([string]$Message) {
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    Add-Content -LiteralPath $logPath -Value "[$((Get-Date).ToString('s'))] $Message"
}

function Resolve-WifiAlias {
    if ($script:WifiAlias) { return }
    $adapter = Get-NetAdapter -Physical -ErrorAction Stop |
        Where-Object { $_.InterfaceDescription -match '(?i)(wi-?fi|wireless|802\.11|wlan)' } |
        Select-Object -First 1
    if (-not $adapter) { throw "No physical Wi-Fi adapter was found." }
    $script:WifiAlias = [string]$adapter.Name
}

function Enable-WifiPath {
    Resolve-WifiAlias
    $adapter = Get-NetAdapter -Name $WifiAlias -ErrorAction Stop
    if ($adapter.Status -eq "Disabled") {
        Enable-NetAdapter -Name $WifiAlias -Confirm:$false
        Start-Sleep -Seconds 2
    }
    if ($WifiProfile) {
        & netsh.exe wlan connect name="$WifiProfile" interface="$WifiAlias" | Out-Null
    }

    foreach ($attempt in 1..30) {
        $route = Get-NetRoute -AddressFamily IPv4 -InterfaceAlias $WifiAlias -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
            Where-Object NextHop -ne "0.0.0.0" |
            Sort-Object RouteMetric, InterfaceMetric |
            Select-Object -First 1
        $address = Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias $WifiAlias -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike "169.254.*" } |
            Select-Object -First 1
        if ($route -and $address) {
            return [pscustomobject]@{
                InterfaceIndex = [int]$route.InterfaceIndex
                Gateway = [string]$route.NextHop
                Address = [string]$address.IPAddress
            }
        }
        Start-Sleep -Seconds 1
    }
    throw "Wi-Fi '$WifiAlias' has no usable IPv4 default route. Connect it to the main router and retry."
}

function Find-TunnelEndpoint {
    if ($TunnelEndpoint) {
        if (-not (Test-IsPublicIPv4 $TunnelEndpoint)) { throw "TunnelEndpoint is not a public IPv4 address: $TunnelEndpoint" }
        return $TunnelEndpoint
    }

    $physicalDefault = Get-NetRoute -AddressFamily IPv4 -InterfaceAlias $EthernetAlias -DestinationPrefix "0.0.0.0/0" -ErrorAction Stop |
        Where-Object NextHop -ne "0.0.0.0" |
        Sort-Object RouteMetric, InterfaceMetric |
        Select-Object -First 1
    if (-not $physicalDefault) { throw "No IPv4 default route was found on $EthernetAlias." }

    $candidate = Get-NetRoute -AddressFamily IPv4 -InterfaceAlias $EthernetAlias -ErrorAction SilentlyContinue |
        Where-Object {
            $_.DestinationPrefix.EndsWith("/32") -and
            $_.NextHop -eq $physicalDefault.NextHop -and
            (Test-IsPublicIPv4 ($_.DestinationPrefix.Split('/')[0]))
        } |
        Sort-Object RouteMetric, InterfaceMetric |
        Select-Object -First 1
    if (-not $candidate) {
        throw "Could not infer the active tunnel endpoint. Pass -TunnelEndpoint explicitly."
    }
    return [string]$candidate.DestinationPrefix.Split('/')[0]
}

function Set-EndpointRoute([int]$WifiIndex, [string]$WifiGateway, [string]$Endpoint) {
    $prefix = "$Endpoint/32"
    $existing = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix -InterfaceIndex $WifiIndex -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
        Where-Object NextHop -eq $WifiGateway |
        Sort-Object RouteMetric |
        Select-Object -First 1
    if ($existing) { return }
    Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix -InterfaceIndex $WifiIndex -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
        Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    New-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix -InterfaceIndex $WifiIndex -NextHop $WifiGateway -RouteMetric 1 -PolicyStore ActiveStore | Out-Null
}

function Test-Guard([int]$WifiIndex, [string]$Endpoint) {
    $selected = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "$Endpoint/32" -ErrorAction Stop |
        Sort-Object @{ Expression = { $_.RouteMetric + $_.InterfaceMetric } } |
        Select-Object -First 1
    if ([int]$selected.InterfaceIndex -ne $WifiIndex) {
        throw "Tunnel endpoint route did not select Wi-Fi: $($selected.InterfaceAlias) via $($selected.NextHop)."
    }

    $cudyRoute = Find-NetRoute -RemoteIPAddress $CudyAddress -ErrorAction Stop | Select-Object -First 1
    if ([string]$cudyRoute.InterfaceAlias -ne $EthernetAlias) {
        throw "Cudy management route is not on ${EthernetAlias}: $($cudyRoute.InterfaceAlias)."
    }
    $tunnel = Get-NetAdapter -Name $TunnelAlias -ErrorAction Stop
    if ($tunnel.Status -ne "Up") { throw "Tunnel adapter '$TunnelAlias' is not Up." }

    & curl.exe -4 --silent --show-error --connect-timeout 5 --max-time 12 $RequiredUrl | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Required URL is not reachable through the maintenance tunnel: $RequiredUrl" }
}

if (-not (Test-IsAdministrator)) {
    $forward = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Quote-Argument $PSCommandPath),
        "-WifiAlias", (Quote-Argument $WifiAlias),
        "-EthernetAlias", (Quote-Argument $EthernetAlias),
        "-TunnelAlias", (Quote-Argument $TunnelAlias),
        "-CudyAddress", (Quote-Argument $CudyAddress),
        "-RequiredUrl", (Quote-Argument $RequiredUrl),
        "-IntervalSeconds", [string]$IntervalSeconds
    )
    if ($WifiProfile) { $forward += @("-WifiProfile", (Quote-Argument $WifiProfile)) }
    if ($TunnelEndpoint) { $forward += @("-TunnelEndpoint", (Quote-Argument $TunnelEndpoint)) }
    if ($Monitor) { $forward += "-Monitor" }
    $process = Start-Process powershell.exe -Verb RunAs -ArgumentList ($forward -join " ") -Wait -PassThru
    exit $process.ExitCode
}

New-Item -ItemType Directory -Path $stateDir -Force | Out-Null

if ($Monitor) {
    Write-GuardLog "monitor started pid=$PID endpoint=$TunnelEndpoint"
    while (Test-Path -LiteralPath $statePath) {
        try {
            $wifi = Enable-WifiPath
            Set-EndpointRoute -WifiIndex $wifi.InterfaceIndex -WifiGateway $wifi.Gateway -Endpoint $TunnelEndpoint
            $tunnel = Get-NetAdapter -Name $TunnelAlias -ErrorAction Stop
            if ($tunnel.Status -ne "Up") { throw "tunnel adapter is $($tunnel.Status)" }
        } catch {
            Write-GuardLog "monitor repair failed: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds ([Math]::Max(5, $IntervalSeconds))
    }
    Write-GuardLog "monitor stopped"
    exit 0
}

$wifi = $null
$endpoint = ""
$routeAdded = $false
try {
    $wifi = Enable-WifiPath
    $endpoint = Find-TunnelEndpoint
    Set-EndpointRoute -WifiIndex $wifi.InterfaceIndex -WifiGateway $wifi.Gateway -Endpoint $endpoint
    $routeAdded = $true
    Start-Sleep -Seconds 2
    Test-Guard -WifiIndex $wifi.InterfaceIndex -Endpoint $endpoint

    $state = [ordered]@{
        armed_at = [datetimeoffset]::UtcNow.ToString("o")
        wifi_alias = $WifiAlias
        wifi_interface_index = $wifi.InterfaceIndex
        wifi_gateway = $wifi.Gateway
        wifi_address = $wifi.Address
        wifi_profile = $WifiProfile
        ethernet_alias = $EthernetAlias
        tunnel_alias = $TunnelAlias
        tunnel_endpoint = $endpoint
        cudy_address = $CudyAddress
        required_url = $RequiredUrl
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8

    $monitorArgs = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Quote-Argument $PSCommandPath),
        "-Monitor",
        "-WifiAlias", (Quote-Argument $WifiAlias),
        "-EthernetAlias", (Quote-Argument $EthernetAlias),
        "-TunnelAlias", (Quote-Argument $TunnelAlias),
        "-TunnelEndpoint", (Quote-Argument $endpoint),
        "-CudyAddress", (Quote-Argument $CudyAddress),
        "-RequiredUrl", (Quote-Argument $RequiredUrl),
        "-IntervalSeconds", [string]$IntervalSeconds
    )
    if ($WifiProfile) { $monitorArgs += @("-WifiProfile", (Quote-Argument $WifiProfile)) }
    $monitorProcess = Start-Process powershell.exe -WindowStyle Hidden -ArgumentList ($monitorArgs -join " ") -PassThru
    Set-Content -LiteralPath $pidPath -Value $monitorProcess.Id -Encoding ASCII

    Write-GuardLog "armed wifi=$WifiAlias gateway=$($wifi.Gateway) endpoint=$endpoint monitor=$($monitorProcess.Id)"
    Write-Host "Cudy maintenance guard is ARMED." -ForegroundColor Green
    Write-Host "Wi-Fi: $($wifi.Address) via $($wifi.Gateway)"
    Write-Host "Maintenance tunnel endpoint: $endpoint via $WifiAlias"
    Write-Host "Cudy management: $CudyAddress via $EthernetAlias"
    Write-Host "Monitor PID: $($monitorProcess.Id)"
    Write-Host "Stop with: powershell -ExecutionPolicy Bypass -File tools\Stop-CudyMaintenanceGuard.ps1"
} catch {
    if ($routeAdded -and $wifi -and $endpoint) {
        Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "$endpoint/32" -InterfaceIndex $wifi.InterfaceIndex -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
            Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    Write-GuardLog "arm failed: $($_.Exception.Message)"
    Write-Error "Cudy maintenance guard was not armed: $($_.Exception.Message)"
    exit 1
}
