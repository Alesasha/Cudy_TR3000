param(
    [string]$Code = "",
    [string]$DeviceId = "",
    [string]$DisplayName = "Windows PC",
    [bool]$StartNow = $true
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run Install-UniversalAgent.ps1 from PowerShell as Administrator."
}

Write-Host "== activate device =="
& "$PSScriptRoot\Enroll-Agent.ps1" -Code $Code -DeviceId $DeviceId -DisplayName $DisplayName

Write-Host "`n== install sing-box runtime =="
& "$PSScriptRoot\Install-SingBoxRuntime.ps1"

Write-Host "`n== install managed agent task =="
$taskArgs = @{
    NoDirectTransports = $true
    PollSeconds = 60
    LocalPort = 18765
}
if ($StartNow) {
    $taskArgs.RunNow = $true
}
& "$PSScriptRoot\Install-ManagedAgentTask.ps1" @taskArgs

if ($StartNow) {
    Write-Host "`nInstall complete. Cudy Agent is starting."
} else {
    Write-Host "`nInstall complete. The agent is configured but remains OFF until it is started manually."
}
