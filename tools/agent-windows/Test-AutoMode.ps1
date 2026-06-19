param(
    [string]$Domain = "ifconfig.me",
    [string]$ExpectedServer = "proxyde",
    [string]$ExpectedInterface = "proxyde",
    [string]$AktauInterface = "AmneziaVPN",
    [string]$ProbeUrl = "https://ifconfig.me/ip",
    [string]$ExpectedEgressIp = "104.194.158.155",
    [switch]$SkipApply
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\agent.env.ps1"

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-AgentConfig {
    $output = & python "$PSScriptRoot\route_agent.py" config --json 2>&1
    $exitCode = $LASTEXITCODE
    $text = ($output | Out-String).Trim()
    if ($exitCode -ne 0) {
        throw "route_agent.py config failed with exit code $exitCode. $text"
    }
    return $text | ConvertFrom-Json
}

function Get-Ipv4Addresses {
    param([string]$Name)
    [System.Net.Dns]::GetHostAddresses($Name) |
        Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork } |
        ForEach-Object { $_.IPAddressToString } |
        Sort-Object -Unique
}

Write-Host "== control config =="
$config = Invoke-AgentConfig
$domainRoute = @($config.domain_routes | Where-Object { $_.domain -eq $Domain } | Select-Object -First 1)
if (-not $domainRoute) {
    throw "No domain route for $Domain in agent config."
}
$transport = @($config.transport_plan | Where-Object { $_.server_id -eq $ExpectedServer } | Select-Object -First 1)
if (-not $transport) {
    throw "No transport_plan item for $ExpectedServer."
}
Write-Host "domain=$($domainRoute.domain) requested=$($domainRoute.requested_server_id) resolved=$($domainRoute.server_id)"
Write-Host "transport=$($transport.server_id) type=$($transport.transport_type) iface=$($transport.interface_name)"
if ($domainRoute.server_id -ne $ExpectedServer) {
    throw "Expected $Domain to resolve to $ExpectedServer, got $($domainRoute.server_id)."
}
if ($transport.interface_name -ne $ExpectedInterface) {
    throw "Expected $ExpectedServer interface $ExpectedInterface, got $($transport.interface_name)."
}

if (-not $SkipApply) {
    if (Test-Admin) {
        Write-Host ""
        Write-Host "== apply managed policy =="
        & "$PSScriptRoot\Start-ManagedAgent.ps1" `
            -NoDirectTransports `
            -ExtraInterfaceMap "aktau=$AktauInterface" `
            -Once
    } else {
        Write-Warning "Not running as Administrator; skipping route apply. Re-run elevated for the full test."
    }
}

Write-Host ""
Write-Host "== resolved routes =="
$ips = @(Get-Ipv4Addresses -Name $Domain)
if ($ips.Count -eq 0) {
    throw "Could not resolve $Domain to IPv4."
}
foreach ($ip in $ips) {
    $route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "$ip/32" -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric, InterfaceMetric |
        Select-Object -First 1
    if ($route) {
        Write-Host "$ip -> $($route.InterfaceAlias) $($route.DestinationPrefix)"
    } else {
        Write-Host "$ip -> no /32 route"
    }
}

Write-Host ""
Write-Host "== transport adapter =="
Get-NetAdapter -Name $ExpectedInterface -ErrorAction SilentlyContinue |
    Select-Object Name, Status, InterfaceIndex, InterfaceDescription |
    Format-Table -AutoSize

Write-Host ""
Write-Host "== probe =="
$probeOutput = & curl.exe -4 -sS --max-time 20 $ProbeUrl 2>$null
$probeExit = $LASTEXITCODE
$probeText = ($probeOutput | Out-String).Trim()
Write-Host $probeText
if ($probeExit -ne 0) {
    throw "Probe failed with exit code $probeExit."
}
if ($ExpectedEgressIp -and $probeText -notmatch [regex]::Escape($ExpectedEgressIp)) {
    throw "Expected egress IP $ExpectedEgressIp, got: $probeText"
}

Write-Host ""
Write-Host "Auto mode smoke test passed."
