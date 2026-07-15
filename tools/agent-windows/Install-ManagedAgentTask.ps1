param(
    [string]$TaskName = "Cudy Managed Route Agent",
    [string]$ServerId = "",
    [string]$InterfaceAlias = "",
    [string[]]$DirectTransport = @(),
    [string[]]$VpnTypeTransport = @(),
    [string[]]$LokVpnTransport = @(),
    [string[]]$SingBoxTransport = @(),
    [string[]]$ExtraInterfaceMap = @(),
    [int]$PollSeconds = 60,
    [int]$LocalPort = 18765,
    [string]$LogPath = "$PSScriptRoot\managed-agent.log",
    [switch]$NoControlTransportPlan,
    [switch]$NoDirectTransports,
    [switch]$VerboseRoutes,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Install-ManagedAgentTask.ps1 must be run as Administrator."
    }
}

function Quote-Arg {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ($Value -notmatch "[\s`"`']") {
        return $Value
    }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Add-Arg {
    param(
        [Parameter(Mandatory = $true)][System.Collections.Generic.List[string]]$Args,
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$Value = $null,
        [switch]$IsSwitch
    )
    $Args.Add($Name) | Out-Null
    if (-not $IsSwitch) {
        $Args.Add((Quote-Arg $Value)) | Out-Null
    }
}

Assert-Admin

$script = Join-Path $PSScriptRoot "Start-ManagedAgent.ps1"
if (-not (Test-Path -LiteralPath $script)) {
    throw "Script not found: $script"
}
if ($ServerId -or $InterfaceAlias) {
    if (-not $ServerId -or -not $InterfaceAlias) {
        throw "ServerId and InterfaceAlias must be passed together."
    }
}

$argsList = [System.Collections.Generic.List[string]]::new()
$argsList.Add("-WindowStyle") | Out-Null
$argsList.Add("Hidden") | Out-Null
$argsList.Add("-NoProfile") | Out-Null
$argsList.Add("-NonInteractive") | Out-Null
$argsList.Add("-ExecutionPolicy") | Out-Null
$argsList.Add("Bypass") | Out-Null
Add-Arg -Args $argsList -Name "-File" -Value $script
Add-Arg -Args $argsList -Name "-PollSeconds" -Value ([string]$PollSeconds)
Add-Arg -Args $argsList -Name "-LocalPort" -Value ([string]$LocalPort)
Add-Arg -Args $argsList -Name "-LogPath" -Value $LogPath
if ($NoControlTransportPlan) { Add-Arg -Args $argsList -Name "-NoControlTransportPlan" -IsSwitch }
if ($NoDirectTransports) { Add-Arg -Args $argsList -Name "-NoDirectTransports" -IsSwitch }
if ($VerboseRoutes) { Add-Arg -Args $argsList -Name "-VerboseRoutes" -IsSwitch }
if ($ServerId -and $InterfaceAlias) {
    Add-Arg -Args $argsList -Name "-ServerId" -Value $ServerId
    Add-Arg -Args $argsList -Name "-InterfaceAlias" -Value $InterfaceAlias
}
foreach ($item in $DirectTransport) { Add-Arg -Args $argsList -Name "-DirectTransport" -Value $item }
foreach ($item in $VpnTypeTransport) { Add-Arg -Args $argsList -Name "-VpnTypeTransport" -Value $item }
foreach ($item in $LokVpnTransport) { Add-Arg -Args $argsList -Name "-LokVpnTransport" -Value $item }
foreach ($item in $SingBoxTransport) { Add-Arg -Args $argsList -Name "-SingBoxTransport" -Value $item }
foreach ($item in $ExtraInterfaceMap) { Add-Arg -Args $argsList -Name "-ExtraInterfaceMap" -Value $item }

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($argsList -join " ")
$trigger = New-ScheduledTaskTrigger -AtLogOn
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principalDef = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 30) `
    -MultipleInstances IgnoreNew `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$settings.Hidden = $true

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principalDef `
    -Settings $settings `
    -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Action:"
Write-Host "  powershell.exe $($argsList -join ' ')"
if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started scheduled task: $TaskName"
} else {
    Write-Host "Start it now with:"
    Write-Host "  Start-ScheduledTask -TaskName `"$TaskName`""
}
