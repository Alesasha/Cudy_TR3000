param(
    [string]$Code = "",
    [string]$DeviceId = "",
    [string]$DisplayName = "Windows PC",
    [bool]$StartNow = $true,
    [switch]$InstallInPlace
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run Install-UniversalAgent.ps1 from PowerShell as Administrator."
}

$installDir = Join-Path $env:ProgramFiles "Cudy Agent"
$sourceDir = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$targetDir = [IO.Path]::GetFullPath($installDir).TrimEnd('\')
if (-not $InstallInPlace -and -not [string]::Equals($sourceDir, $targetDir, [StringComparison]::OrdinalIgnoreCase)) {
    Stop-ScheduledTask -TaskName "Cudy Managed Route Agent" -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    $skip = @("logs", "run", "transports", "updates")
    Get-ChildItem -LiteralPath $sourceDir -Force | ForEach-Object {
        if ($_.Name -in $skip) { return }
        $destination = Join-Path $targetDir $_.Name
        if (Test-Path -LiteralPath $destination) {
            Remove-Item -LiteralPath $destination -Recurse -Force
        }
        Copy-Item -LiteralPath $_.FullName -Destination $destination -Recurse -Force
    }
    & (Join-Path $targetDir "Install-UniversalAgent.ps1") `
        -Code $Code `
        -DeviceId $DeviceId `
        -DisplayName $DisplayName `
        -StartNow:$StartNow `
        -InstallInPlace
    exit $LASTEXITCODE
}

if ($Code -or -not (Test-Path -LiteralPath (Join-Path $PSScriptRoot "agent.env.ps1"))) {
    Write-Host "== activate device =="
    & "$PSScriptRoot\Enroll-Agent.ps1" -Code $Code -DeviceId $DeviceId -DisplayName $DisplayName
} else {
    Write-Host "== keep existing device activation =="
}

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

Write-Host "`n== install desktop UI =="
& "$PSScriptRoot\Install-AgentUi.ps1"

Write-Host "`n== register installed application =="
& "$PSScriptRoot\Register-CudyAgentInstallation.ps1"

if ($StartNow) {
    Write-Host "`nInstall complete. Cudy Agent is starting. Open it from the desktop shortcut."
} else {
    Write-Host "`nInstall complete. The agent remains OFF. Use the Cudy Agent desktop shortcut to start it."
}
