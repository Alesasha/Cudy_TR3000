param(
    [string]$AgentTaskName = "Cudy Managed Route Agent",
    [int]$FailureThreshold = 3,
    [int]$HeartbeatMaxAgeSeconds = 240,
    [int]$RestartCooldownSeconds = 600,
    [string[]]$ProbeUrls = @(
        "https://www.msftconnecttest.com/connecttest.txt",
        "https://connectivitycheck.gstatic.com/generate_204",
        "https://ifconfig.me/ip"
    ),
    [string]$ServiceConfigPath = "$PSScriptRoot\watchdog-services.json",
    [string]$ManagedConfigPath = "$PSScriptRoot\run\fresh-config.json",
    [string]$RequiredDevelopmentUrl = "",
    [switch]$ProbeOnly
)

$ErrorActionPreference = "Stop"
$runDir = Join-Path $PSScriptRoot "run"
$logDir = Join-Path $PSScriptRoot "logs"
$statePath = Join-Path $runDir "watchdog-state.json"
$armedPath = Join-Path $runDir "watchdog.armed"
$keepalivePath = Join-Path $runDir "watchdog.keepalive"
$trippedPath = Join-Path $runDir "watchdog.tripped.json"
$heartbeatPath = Join-Path $runDir "agent-heartbeat.json"
$agentLogPath = Join-Path $PSScriptRoot "managed-agent.log"
$watchdogLogPath = Join-Path $logDir "watchdog.log"
$emergencyScript = Join-Path $PSScriptRoot "Emergency-Stop-Agent.ps1"
$pendingReportPath = Join-Path $runDir "watchdog-report-pending.json"
$agentEnvPath = Join-Path $PSScriptRoot "agent.env.ps1"
if (Test-Path -LiteralPath $agentEnvPath) {
    . $agentEnvPath
}

function Write-WatchdogLog {
    param([string]$Message, [string]$Level = "INFO")
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    Add-Content -LiteralPath $watchdogLogPath -Value "[$((Get-Date).ToString('s'))] [$Level] $Message"
}

function Write-JsonAtomic {
    param([string]$Path, $Value)
    $tempPath = "$Path.tmp"
    $json = $Value | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText($tempPath, $json, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tempPath -Destination $Path -Force
}

function Read-State {
    $empty = [ordered]@{
        consecutive_failures = 0
        last_probe_at = $null
        last_success_at = $null
        last_failure_at = $null
        last_restart_at = $null
        last_result = "unknown"
        probe_results = @()
    }
    if (-not (Test-Path -LiteralPath $statePath)) {
        return $empty
    }
    try {
        $saved = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
        foreach ($name in @($empty.Keys)) {
            if ($null -ne $saved.$name) { $empty[$name] = $saved.$name }
        }
    } catch {
        Write-WatchdogLog "Ignoring unreadable watchdog state: $($_.Exception.Message)" "WARN"
    }
    return $empty
}

function Test-TcpProbe {
    param([string]$HostName = "1.1.1.1", [int]$Port = 443, [int]$TimeoutMilliseconds = 3500)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $result = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne($TimeoutMilliseconds, $false)) {
            return $false
        }
        $client.EndConnect($result)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Test-WebProbe {
    param(
        [string]$Url,
        [string]$SuccessPattern = "",
        [string]$FailurePattern = ""
    )
    $tempPath = Join-Path $runDir ("watchdog-web-{0}.tmp" -f ([guid]::NewGuid().ToString("N")))
    try {
        $httpCode = & curl.exe -4 --location --silent --show-error --connect-timeout 4 --max-time 10 --range 0-262143 --output $tempPath --write-out "%{http_code}" $Url 2>$null
        $parsedCode = 0
        $parsed = [int]::TryParse([string]$httpCode, [ref]$parsedCode)
        $curlOK = ($LASTEXITCODE -eq 0 -and $parsed -and $parsedCode -gt 0)
        $body = ""
        if (Test-Path -LiteralPath $tempPath) {
            $body = Get-Content -Raw -LiteralPath $tempPath -ErrorAction SilentlyContinue
            if ($body.Length -gt 262144) { $body = $body.Substring(0, 262144) }
        }
        $successMatched = (-not $SuccessPattern) -or ($body -match $SuccessPattern)
        $failureMatched = [bool]($FailurePattern -and ($body -match $FailurePattern))
        return [pscustomobject]@{
            ok = [bool]($curlOK -and $successMatched -and -not $failureMatched)
            http_code = $parsedCode
            success_matched = [bool]$successMatched
            failure_matched = $failureMatched
        }
    } catch {
        return [pscustomobject]@{ ok = $false; http_code = 0; success_matched = $false; failure_matched = $false }
    } finally {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
}

function Get-CriticalServices {
    $services = [System.Collections.Generic.List[object]]::new()
    $seen = @{}
    function Add-ServiceTargets {
        param($Item, [string]$Source)
        $name = if ([string]$Item.label) { [string]$Item.label } elseif ([string]$Item.name) { [string]$Item.name } else { [string]$Item.service_key }
        $targets = @()
        if ($Item.targets) { $targets = @($Item.targets) }
        elseif ([string]$Item.url) { $targets = @([string]$Item.url) }
        foreach ($target in $targets) {
            $url = [string]$target
            if (-not $url) { continue }
            $key = "$name`n$url"
            if ($seen.ContainsKey($key)) { continue }
            $seen[$key] = $true
            $services.Add([pscustomobject]@{
                name = $(if ($name) { $name } else { $url })
                url = $url
                critical = ($Item.critical -ne $false)
                success_pattern = [string]$Item.success_pattern
                failure_pattern = [string]$Item.failure_pattern
                source = $Source
            }) | Out-Null
        }
    }
    if (Test-Path -LiteralPath $ManagedConfigPath) {
        try {
            $managed = Get-Content -Raw -LiteralPath $ManagedConfigPath | ConvertFrom-Json
            foreach ($item in @($managed.critical_services)) {
                Add-ServiceTargets -Item $item -Source "control-server"
            }
        } catch {
            Write-WatchdogLog "Cannot read managed critical services: $($_.Exception.Message)" "ERROR"
        }
    }
    if (Test-Path -LiteralPath $ServiceConfigPath) {
        try {
            $config = Get-Content -Raw -LiteralPath $ServiceConfigPath | ConvertFrom-Json
            foreach ($item in @($config.services)) {
                Add-ServiceTargets -Item $item -Source "local"
            }
        } catch {
            Write-WatchdogLog "Cannot read critical service config: $($_.Exception.Message)" "ERROR"
        }
    }
    if ($RequiredDevelopmentUrl -and -not (@($services | Where-Object url -eq $RequiredDevelopmentUrl).Count)) {
        $services.Add([pscustomobject]@{
            name = "Development service"
            url = $RequiredDevelopmentUrl
            critical = $true
            success_pattern = ""
            failure_pattern = ""
            source = "legacy"
        }) | Out-Null
    }
    return $services.ToArray()
}

function Send-WatchdogReport {
    param($Report)
    $token = [string]$env:VPN_AGENT_TOKEN
    $controlUrl = [string]$env:VPN_CONTROL_URL
    if (-not $token -or -not $controlUrl) { return $false }
    try {
        $body = @{
            summary = "Agent watchdog: critical connectivity failure"
            report = ($Report | ConvertTo-Json -Depth 10)
        } | ConvertTo-Json -Depth 12
        Invoke-RestMethod `
            -Method Post `
            -Uri ($controlUrl.TrimEnd("/") + "/api/agent/diagnostics") `
            -Headers @{ Authorization = "Bearer $token" } `
            -ContentType "application/json; charset=utf-8" `
            -Body $body `
            -TimeoutSec 8 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Test-Connectivity {
    $results = [System.Collections.Generic.List[object]]::new()
    $tcpOK = Test-TcpProbe
    $results.Add([pscustomobject]@{ target = "tcp://1.1.1.1:443"; ok = $tcpOK }) | Out-Null
    $webOK = $false
    foreach ($url in $ProbeUrls) {
        $probe = Test-WebProbe -Url $url
        $results.Add([pscustomobject]@{ target = $url; ok = $probe.ok; http_code = $probe.http_code }) | Out-Null
        if ($probe.ok) {
            $webOK = $true
            break
        }
    }
    $criticalOK = $true
    $failedServices = [System.Collections.Generic.List[string]]::new()
    $servicesByName = @(Get-CriticalServices) | Group-Object -Property name
    foreach ($serviceGroup in $servicesByName) {
        $serviceOK = $false
        $isCritical = $false
        foreach ($service in @($serviceGroup.Group)) {
            $probe = Test-WebProbe -Url ([string]$service.url) -SuccessPattern ([string]$service.success_pattern) -FailurePattern ([string]$service.failure_pattern)
            if ($probe.ok) { $serviceOK = $true }
            if ([bool]$service.critical) { $isCritical = $true }
            $results.Add([pscustomobject]@{
                target = [string]$service.url
                name = [string]$service.name
                ok = [bool]$probe.ok
                http_code = $probe.http_code
                success_matched = $probe.success_matched
                failure_matched = $probe.failure_matched
                critical = [bool]$service.critical
                source = [string]$service.source
            }) | Out-Null
        }
        if ($isCritical -and -not $serviceOK) {
            $criticalOK = $false
            $failedServices.Add([string]$serviceGroup.Name) | Out-Null
        }
    }
    return [pscustomobject]@{
        ok = ($webOK -and $criticalOK)
        base_internet_ok = $webOK
        tcp_ok = $tcpOK
        critical_services_ok = $criticalOK
        failed_services = $failedServices.ToArray()
        probes = $results.ToArray()
    }
}

function Get-HeartbeatAgeSeconds {
    $timestamp = $null
    if (Test-Path -LiteralPath $heartbeatPath) {
        try {
            $heartbeat = Get-Content -Raw -LiteralPath $heartbeatPath | ConvertFrom-Json
            $timestamp = [datetimeoffset]::Parse([string]$heartbeat.updated_at)
        } catch {
        }
    }
    if ($null -eq $timestamp -and (Test-Path -LiteralPath $agentLogPath)) {
        $timestamp = [datetimeoffset](Get-Item -LiteralPath $agentLogPath).LastWriteTimeUtc
    }
    if ($null -eq $timestamp) {
        return $null
    }
    return [int]([datetimeoffset]::UtcNow - $timestamp.ToUniversalTime()).TotalSeconds
}

function Test-RestartCooldownExpired {
    param($State)
    if (-not $State.last_restart_at) { return $true }
    try {
        $last = [datetimeoffset]::Parse([string]$State.last_restart_at)
        return (([datetimeoffset]::UtcNow - $last).TotalSeconds -ge $RestartCooldownSeconds)
    } catch {
        return $true
    }
}

New-Item -ItemType Directory -Force -Path $runDir, $logDir | Out-Null
$connectivity = Test-Connectivity
if ($ProbeOnly) {
    $connectivity | ConvertTo-Json -Depth 8
    if ($connectivity.ok) { exit 0 } else { exit 2 }
}

if (-not (Test-Path -LiteralPath $armedPath)) {
    exit 0
}
if (Test-Path -LiteralPath $trippedPath) {
    exit 0
}

$state = Read-State
$now = [datetimeoffset]::UtcNow
$state.last_probe_at = $now.ToString("o")
$state.probe_results = @($connectivity.probes)

if (Test-Path -LiteralPath $keepalivePath) {
    Remove-Item -LiteralPath $keepalivePath -Force -ErrorAction SilentlyContinue
    $state.consecutive_failures = 0
    $state.last_result = "keepalive"
    Write-JsonAtomic -Path $statePath -Value $state
    Write-WatchdogLog "Development keepalive marker consumed; failure counter reset."
    exit 0
}

$agentTask = Get-ScheduledTask -TaskName $AgentTaskName -ErrorAction SilentlyContinue
if (-not $agentTask -or $agentTask.State -eq "Disabled") {
    $state.consecutive_failures = 0
    $state.last_result = "agent_disabled"
    Write-JsonAtomic -Path $statePath -Value $state
    exit 0
}

if ($connectivity.ok) {
    $hadFailures = [int]$state.consecutive_failures -gt 0
    $state.consecutive_failures = 0
    $state.last_success_at = $now.ToString("o")
    $state.last_result = "healthy"
    if ($hadFailures) {
        Write-WatchdogLog "Connectivity recovered before emergency action."
    }
    if (Test-Path -LiteralPath $pendingReportPath) {
        try {
            $pendingReport = Get-Content -Raw -LiteralPath $pendingReportPath | ConvertFrom-Json
            if (Send-WatchdogReport -Report $pendingReport) {
                Remove-Item -LiteralPath $pendingReportPath -Force -ErrorAction SilentlyContinue
                Write-WatchdogLog "Queued watchdog report delivered to control-server."
            }
        } catch {
        }
    }

    $heartbeatAge = Get-HeartbeatAgeSeconds
    if (($null -eq $heartbeatAge -or $heartbeatAge -gt $HeartbeatMaxAgeSeconds) -and (Test-RestartCooldownExpired -State $state)) {
        Write-WatchdogLog "Agent heartbeat is stale (age=$heartbeatAge); restarting scheduled task." "WARN"
        Stop-ScheduledTask -TaskName $AgentTaskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Start-ScheduledTask -TaskName $AgentTaskName -ErrorAction Stop
        $state.last_restart_at = $now.ToString("o")
        $state.last_result = "agent_restarted"
    }
    Write-JsonAtomic -Path $statePath -Value $state
    exit 0
}

$state.consecutive_failures = [int]$state.consecutive_failures + 1
$state.last_failure_at = $now.ToString("o")
$state.last_result = "connectivity_failed"
Write-WatchdogLog "Connectivity failure $($state.consecutive_failures)/$FailureThreshold; base_ok=$($connectivity.base_internet_ok) failed_services=$($connectivity.failed_services -join ',')." "WARN"
Write-JsonAtomic -Path $statePath -Value $state

if ([int]$state.consecutive_failures -lt $FailureThreshold) {
    exit 1
}

if (-not (Test-Path -LiteralPath $emergencyScript)) {
    Write-WatchdogLog "Emergency script is missing: $emergencyScript" "ERROR"
    exit 3
}

$trip = [ordered]@{
    tripped_at = $now.ToString("o")
    reason = "connectivity_failed"
    consecutive_failures = [int]$state.consecutive_failures
    probe_results = @($connectivity.probes)
}
Write-JsonAtomic -Path $trippedPath -Value $trip
if (-not (Send-WatchdogReport -Report $trip)) {
    Write-JsonAtomic -Path $pendingReportPath -Value $trip
}
Write-WatchdogLog "Failure threshold reached; running Emergency-Stop-Agent.ps1 and disarming watchdog." "ERROR"
& $emergencyScript -TaskName $AgentTaskName
Remove-Item -LiteralPath $armedPath -Force -ErrorAction SilentlyContinue
exit 4
