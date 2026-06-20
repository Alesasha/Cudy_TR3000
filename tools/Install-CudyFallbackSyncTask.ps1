param(
    [string]$TaskName = "Cudy Fallback Control Sync",
    [int]$EveryMinutes = 30,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Quote-Arg {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ($Value -notmatch "[\s`"`']") {
        return $Value
    }
    return '"' + $Value.Replace('"', '\"') + '"'
}

$isAdmin = Assert-Admin

$runner = Join-Path $PSScriptRoot "Run-CudyFallbackSync.ps1"
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Runner not found: $runner"
}

$argsList = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", (Quote-Arg $runner)
)

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($argsList -join " ")
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At ((Get-Date).Date.AddMinutes(5)) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 1)
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principalArgs = @{
    UserId = $currentUser
    LogonType = "Interactive"
}
if ($isAdmin) {
    $principalArgs.RunLevel = "Highest"
}
$principalDef = New-ScheduledTaskPrincipal @principalArgs
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principalDef `
    -Settings $settings `
    -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Repeat every: $EveryMinutes minutes"
Write-Host "Action:"
Write-Host "  powershell.exe $($argsList -join ' ')"
if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started scheduled task: $TaskName"
}
