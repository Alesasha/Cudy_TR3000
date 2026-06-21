param(
    [string]$TaskName = "Cudy Managed Route Agent",
    [string]$LogPath = "$PSScriptRoot\managed-agent.log",
    [int]$LocalPort = 18765,
    [int]$Tail = 60,
    [switch]$Network,
    [string]$VpnInterfaceAlias = "AmneziaVPN"
)

$ErrorActionPreference = "Stop"

function Format-TaskResult {
    param([int]$Code)
    $hex = "0x{0:X}" -f $Code
    $message = switch ($Code) {
        0 { "Success" }
        267008 { "Ready" }
        267009 { "Running" }
        267010 { "Disabled" }
        267011 { "Queued" }
        267012 { "Terminated" }
        267013 { "No more runs" }
        267014 { "No trigger" }
        default { "Unknown" }
    }
    return "$Code ($hex, $message)"
}

Write-Host "== scheduled task =="
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "Task not installed: $TaskName"
} else {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    [pscustomobject]@{
        TaskName = $task.TaskName
        State = $task.State
        LastRunTime = $info.LastRunTime
        LastTaskResult = Format-TaskResult -Code $info.LastTaskResult
        NextRunTime = $info.NextRunTime
        NumberOfMissedRuns = $info.NumberOfMissedRuns
    } | Format-List
}

Write-Host ""
Write-Host "== processes =="
Get-Process powershell, pwsh, ssh, sing-box -ErrorAction SilentlyContinue |
    Select-Object Id, ProcessName, StartTime, Path |
    Sort-Object ProcessName, StartTime |
    Format-Table -AutoSize

Write-Host ""
Write-Host "== control tunnel =="
Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, LocalPort, OwningProcess |
    Format-Table -AutoSize

Write-Host ""
Write-Host "== log tail =="
if (Test-Path -LiteralPath $LogPath) {
    Get-Content -LiteralPath $LogPath -Tail $Tail
} else {
    Write-Host "Log not found: $LogPath"
}

if ($Network) {
    Write-Host ""
    Write-Host "== network diagnostics =="
    & "$PSScriptRoot\Check-Net.ps1" -VpnInterfaceAlias $VpnInterfaceAlias
}
