param(
    [string]$TaskName = "Cudy Managed Route Agent",
    [string]$Domain = "ifconfig.me",
    [string]$ExpectedServer = "proxyde",
    [string]$ExpectedInterface = "proxyde",
    [string]$ExpectedEgressIp = "",
    [int]$LocalPort = 18765,
    [string]$LogPath = "$PSScriptRoot\managed-agent.log",
    [int]$RouteWaitSeconds = 45,
    [int]$ProbeRetries = 3,
    [switch]$SkipProbe,
    [switch]$RequireProbe
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\agent.env.ps1"

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

function Get-BestRoute {
    param([string]$Ip)
    $route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "$Ip/32" -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric, InterfaceMetric |
        Select-Object -First 1
    if ($route) { return $route }
    $probe = Find-NetRoute -RemoteIPAddress $Ip -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric, InterfaceMetric |
        Select-Object -First 1
    return $probe
}

Write-Host "== scheduled task =="
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    throw "Scheduled task is not installed: $TaskName"
}
$info = Get-ScheduledTaskInfo -TaskName $TaskName
[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State
    LastRunTime = $info.LastRunTime
    LastTaskResult = $info.LastTaskResult
} | Format-List
if ($task.State -ne "Running") {
    throw "Scheduled task is not running: $($task.State)"
}

Write-Host ""
Write-Host "== control tunnel =="
$listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $listener) {
    throw "Control tunnel is not listening on 127.0.0.1:$LocalPort"
}
$listener | Select-Object LocalAddress, LocalPort, OwningProcess | Format-Table -AutoSize

Write-Host ""
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
[pscustomobject]@{
    Domain = $domainRoute.domain
    Requested = $domainRoute.requested_server_id
    Resolved = $domainRoute.server_id
    Transport = $transport.server_id
    Type = $transport.transport_type
    Interface = $transport.interface_name
} | Format-List
if ($domainRoute.server_id -ne $ExpectedServer) {
    throw "Expected $Domain to resolve to $ExpectedServer, got $($domainRoute.server_id)."
}
if ($transport.interface_name -ne $ExpectedInterface) {
    throw "Expected $ExpectedServer interface $ExpectedInterface, got $($transport.interface_name)."
}

Write-Host ""
Write-Host "== transport adapter =="
$adapter = Get-NetAdapter -Name $ExpectedInterface -ErrorAction SilentlyContinue
if (-not $adapter) {
    throw "Adapter is not present: $ExpectedInterface"
}
$adapter | Select-Object Name, Status, InterfaceIndex, InterfaceDescription | Format-Table -AutoSize
if ($adapter.Status -ne "Up") {
    throw "Adapter $ExpectedInterface is not Up: $($adapter.Status)"
}

Write-Host ""
Write-Host "== route check =="
$ips = @(Get-Ipv4Addresses -Name $Domain)
if ($ips.Count -eq 0) {
    throw "Could not resolve $Domain to IPv4."
}
$deadline = (Get-Date).AddSeconds($RouteWaitSeconds)
do {
    $routeRows = foreach ($ip in $ips) {
        $route = Get-BestRoute -Ip $ip
        if ($route) {
            [pscustomobject]@{
                Target = $ip
                InterfaceAlias = $route.InterfaceAlias
                DestinationPrefix = $route.DestinationPrefix
                NextHop = $route.NextHop
                RouteMetric = $route.RouteMetric
                InterfaceMetric = $route.InterfaceMetric
            }
        } else {
            [pscustomobject]@{
                Target = $ip
                InterfaceAlias = "(none)"
                DestinationPrefix = ""
                NextHop = ""
                RouteMetric = ""
                InterfaceMetric = ""
            }
        }
    }
    $matched = @($routeRows | Where-Object { $_.InterfaceAlias -eq $ExpectedInterface } | Select-Object -First 1)
    if ($matched) { break }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)

$routeRows = foreach ($ip in $ips) {
    $route = Get-BestRoute -Ip $ip
    if ($route) {
        [pscustomobject]@{
            Target = $ip
            InterfaceAlias = $route.InterfaceAlias
            DestinationPrefix = $route.DestinationPrefix
            NextHop = $route.NextHop
            RouteMetric = $route.RouteMetric
            InterfaceMetric = $route.InterfaceMetric
        }
    } else {
        [pscustomobject]@{
            Target = $ip
            InterfaceAlias = "(none)"
            DestinationPrefix = ""
            NextHop = ""
            RouteMetric = ""
            InterfaceMetric = ""
        }
    }
}
$routeRows | Format-Table -AutoSize
if (-not $matched) {
    throw "No $Domain IPv4 route is using $ExpectedInterface after ${RouteWaitSeconds}s."
}

if (-not $SkipProbe) {
    Write-Host ""
    Write-Host "== pinned probe =="
    $probeIp = $matched.Target
    $probeOk = $false
    $probeText = ""
    for ($attempt = 1; $attempt -le $ProbeRetries; $attempt++) {
        $probeOutput = & curl.exe -4 -sS --resolve "$Domain`:443`:$probeIp" --connect-timeout 10 --max-time 25 "https://$Domain/ip" 2>&1
        $probeExit = $LASTEXITCODE
        $probeText = ($probeOutput | Out-String).Trim()
        Write-Host "attempt=$attempt exit=$probeExit $probeText"
        if ($probeExit -eq 0 -and (-not $ExpectedEgressIp -or $probeText -match [regex]::Escape($ExpectedEgressIp))) {
            $probeOk = $true
            break
        }
        Start-Sleep -Seconds 3
    }
    if (-not $probeOk) {
        $message = "Pinned probe did not pass through $ExpectedInterface cleanly. Last result: $probeText"
        if ($RequireProbe -or $ExpectedEgressIp) {
            throw $message
        }
        Write-Warning $message
    }
}

Write-Host ""
Write-Host "== recent agent log =="
if (Test-Path -LiteralPath $LogPath) {
    Get-Content -LiteralPath $LogPath -Tail 20
} else {
    Write-Host "Log not found: $LogPath"
}

Write-Host ""
Write-Host "Production Windows agent smoke test passed."
