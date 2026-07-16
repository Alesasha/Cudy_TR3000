param(
    [string]$StatePath = "C:\ProgramData\CudyVPN\openai-maintenance\state.json",
    [string]$WiFiProfile = ""
)

$ErrorActionPreference = "Stop"
$taskName = "Cudy OpenAI Route Refresh"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Install-OpenAIMaintenanceRefreshTask.ps1 must be run as Administrator."
}
if (-not (Test-Path -LiteralPath $StatePath)) {
    throw "OpenAI maintenance state not found: $StatePath"
}
if ($WiFiProfile) {
    $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    $state | Add-Member -NotePropertyName endpoint_wifi_profile -NotePropertyValue $WiFiProfile -Force
    $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StatePath -Encoding UTF8
}

$source = Join-Path $PSScriptRoot "Update-OpenAIMaintenanceRoutes.ps1"
$stateDir = Split-Path -Parent $StatePath
$installed = Join-Path $stateDir "Update-OpenAIMaintenanceRoutes.ps1"
Copy-Item -LiteralPath $source -Destination $installed -Force

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$installed`" -StatePath `"$StatePath`""
)
$startup = New-ScheduledTaskTrigger -AtStartup
$periodic = New-ScheduledTaskTrigger -Once -At ([datetime]::Now.AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes 2) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger @($startup, $periodic) -Settings $settings `
    -User "SYSTEM" -RunLevel Highest -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
$deadline = [datetime]::UtcNow.AddSeconds(45)
do {
    Start-Sleep -Milliseconds 500
    $task = Get-ScheduledTask -TaskName $taskName
} while ($task.State -eq "Running" -and [datetime]::UtcNow -lt $deadline)

$info = Get-ScheduledTaskInfo -TaskName $taskName
if ($task.State -eq "Running" -or $info.LastTaskResult -ne 0) {
    throw "OpenAI route refresh task verification failed: state=$($task.State) result=$($info.LastTaskResult)"
}
$state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
if (-not $state.endpoint_prefix -or -not $state.endpoint_interface_index -or -not $state.endpoint_next_hop) {
    throw "OpenAI route refresh did not persist endpoint pin metadata."
}
$route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix ([string]$state.endpoint_prefix) `
    -InterfaceIndex ([int]$state.endpoint_interface_index) -ErrorAction SilentlyContinue |
    Where-Object { $_.NextHop -eq [string]$state.endpoint_next_hop } |
    Select-Object -First 1
if (-not $route) { throw "OpenAI endpoint route is missing after refresh task verification." }

Write-Host "Installed and verified scheduled task: $taskName"
Write-Host "Endpoint: $($state.endpoint_prefix) via $($state.endpoint_interface) $($state.endpoint_next_hop)"
