param(
    [string]$TaskName = "Cudy Managed Route Agent",
    [int]$LocalPort = 18765,
    [switch]$StopRunning,
    [switch]$StopTransports,
    [switch]$RestoreDirect,
    [switch]$FullRollback
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Uninstall-ManagedAgentTask.ps1 must be run as Administrator."
}

if ($FullRollback) {
    $StopRunning = $true
    $StopTransports = $true
    $RestoreDirect = $true
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "Task not installed: $TaskName"
} else {
    if ($StopRunning -and $task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName
    }

    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task: $TaskName"
}

if ($StopRunning) {
    Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object {
            $process = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
            if ($process -and $process.ProcessName -in @("ssh", "powershell", "pwsh")) {
                Write-Host "Stopping control tunnel listener pid=$($process.Id) name=$($process.ProcessName) port=$LocalPort"
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
        }
}

if ($StopTransports) {
    & "$PSScriptRoot\Stop-SingBoxTransport.ps1" -All
}

if ($RestoreDirect) {
    & "$PSScriptRoot\Restore-Direct.ps1"
}
