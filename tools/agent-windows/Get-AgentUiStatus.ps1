param(
    [string]$TaskName = "Cudy Managed Route Agent",
    [int]$LocalPort = 18765,
    [string]$OutputPath = "$PSScriptRoot\run\ui-status.json"
)

$ErrorActionPreference = "Stop"
$result = [ordered]@{
    task_installed = $false
    task_state = "Missing"
    control_connected = $false
    traffic_bytes = 0L
    current_version = "unknown"
    error = ""
}

try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        $result.task_installed = $true
        $result.task_state = [string]$task.State
    }
    $listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $result.control_connected = $null -ne $listener
    $adapters = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {
        $_.InterfaceDescription -in @("sing-tun Tunnel", "WireGuard Tunnel") -or
        $_.Name -match '^(proxy|lokvpn-|AmneziaVPN|Nash)'
    }
    foreach ($adapter in $adapters) {
        $stats = Get-NetAdapterStatistics -Name $adapter.Name -ErrorAction SilentlyContinue
        if ($stats) { $result.traffic_bytes += [int64]$stats.ReceivedBytes + [int64]$stats.SentBytes }
    }
    $versionPath = Join-Path $PSScriptRoot "agent.version.json"
    if (Test-Path -LiteralPath $versionPath) {
        $version = Get-Content -Raw -LiteralPath $versionPath | ConvertFrom-Json
        $result.current_version = [string]$version.version_name
    }
} catch {
    $result.error = $_.Exception.Message
}

$parent = Split-Path -Parent $OutputPath
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
[System.IO.File]::WriteAllText($OutputPath, ($result | ConvertTo-Json -Depth 5), [System.Text.UTF8Encoding]::new($false))
