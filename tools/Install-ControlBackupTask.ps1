param(
    [string]$TaskName = "Cudy Control Server Backup",
    [string]$At = "03:15",
    [string]$PasswordFile = "$PSScriptRoot\..\secrets\control_backup_ssh_password.txt",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Install-ControlBackupTask.ps1 must be run as Administrator."
    }
}

function Quote-Arg {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ($Value -notmatch "[\s`"`']") {
        return $Value
    }
    return '"' + $Value.Replace('"', '\"') + '"'
}

Assert-Admin

$runner = Join-Path $PSScriptRoot "Run-ControlBackup.ps1"
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Runner not found: $runner"
}

if (-not (Test-Path -LiteralPath $PasswordFile)) {
    Write-Warning "Password file not found: $PasswordFile"
    Write-Warning "The scheduled task will fail until CONTROL_BACKUP_SSH_PASSWORD is available or this ignored local file is created."
}

$argsList = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", (Quote-Arg $runner),
    "-PasswordFile", (Quote-Arg $PasswordFile)
)

$triggerTime = [DateTime]::ParseExact($At, "HH:mm", [Globalization.CultureInfo]::InvariantCulture)
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($argsList -join " ")
$trigger = New-ScheduledTaskTrigger -Daily -At $triggerTime
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principalDef = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principalDef `
    -Settings $settings `
    -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Daily at: $At"
Write-Host "Action:"
Write-Host "  powershell.exe $($argsList -join ' ')"
if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started scheduled task: $TaskName"
}
