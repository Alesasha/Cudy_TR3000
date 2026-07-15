param(
    [string]$TaskName = "Cudy Agent Safety Watchdog",
    [string]$AgentTaskName = "Cudy Managed Route Agent",
    [int]$FailureThreshold = 3,
    [int]$HeartbeatMaxAgeSeconds = 240,
    [string[]]$CriticalService = @(),
    [string]$RequiredDevelopmentUrl = "",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$installLogDir = Join-Path $PSScriptRoot "logs"
$installLogPath = Join-Path $installLogDir "watchdog-install.log"
New-Item -ItemType Directory -Force -Path $installLogDir | Out-Null
trap {
    Add-Content -LiteralPath $installLogPath -Value "[$((Get-Date).ToString('s'))] ERROR $($_.Exception.Message)"
    throw
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    $arguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"",
        "-TaskName", "`"$TaskName`"",
        "-AgentTaskName", "`"$AgentTaskName`"",
        "-FailureThreshold", [string]$FailureThreshold,
        "-HeartbeatMaxAgeSeconds", [string]$HeartbeatMaxAgeSeconds
    )
    if ($RequiredDevelopmentUrl) { $arguments += @("-RequiredDevelopmentUrl", "`"$RequiredDevelopmentUrl`"") }
    foreach ($service in $CriticalService) { $arguments += @("-CriticalService", "`"$service`"") }
    if ($RunNow) { $arguments += "-RunNow" }
    $process = Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList ($arguments -join " ") -PassThru -Wait
    exit $process.ExitCode
}

$watchdogScript = Join-Path $PSScriptRoot "Watch-AgentConnectivity.ps1"
if (-not (Test-Path -LiteralPath $watchdogScript)) {
    throw "Watchdog script not found: $watchdogScript"
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot "Emergency-Stop-Agent.ps1"))) {
    throw "Emergency-Stop-Agent.ps1 not found in $PSScriptRoot"
}

$runDir = Join-Path $PSScriptRoot "run"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
Set-Content -LiteralPath (Join-Path $runDir "watchdog.armed") -Value ([datetimeoffset]::UtcNow.ToString("o")) -Encoding ASCII
Remove-Item -LiteralPath (Join-Path $runDir "watchdog.tripped.json") -Force -ErrorAction SilentlyContinue

if ($RequiredDevelopmentUrl) {
    $CriticalService += "Development service=$RequiredDevelopmentUrl"
}
if ($CriticalService.Count -gt 0) {
    $services = foreach ($definition in $CriticalService) {
        $parts = ([string]$definition) -split "=", 2
        if ($parts.Count -ne 2 -or -not $parts[0].Trim() -or -not $parts[1].Trim()) {
            throw "CriticalService must use NAME=URL format: $definition"
        }
        [ordered]@{ name = $parts[0].Trim(); url = $parts[1].Trim(); critical = $true }
    }
    $serviceConfig = [ordered]@{ services = @($services) } | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText(
        (Join-Path $PSScriptRoot "watchdog-services.json"),
        $serviceConfig,
        [System.Text.UTF8Encoding]::new($false)
    )
}

$arguments = @(
    "-NoProfile",
    "-NonInteractive",
    "-WindowStyle", "Hidden",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$watchdogScript`"",
    "-AgentTaskName", "`"$AgentTaskName`"",
    "-FailureThreshold", [string]$FailureThreshold,
    "-HeartbeatMaxAgeSeconds", [string]$HeartbeatMaxAgeSeconds
)
$arguments = $arguments -join " "

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$periodicTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$task = New-ScheduledTask -Action $action -Trigger @($startupTrigger, $periodicTrigger) -Settings $settings -Principal $principal
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
}

Write-Host "Installed watchdog task: $TaskName"
Write-Host "Agent task: $AgentTaskName"
Write-Host "Failure threshold: $FailureThreshold consecutive checks"
Write-Host "Heartbeat maximum age: ${HeartbeatMaxAgeSeconds}s"
if ($CriticalService.Count -gt 0) {
    Write-Host "Critical services: $($CriticalService -join '; ')"
}
Add-Content -LiteralPath $installLogPath -Value "[$((Get-Date).ToString('s'))] OK task=$TaskName critical_services=$($CriticalService.Count)"
