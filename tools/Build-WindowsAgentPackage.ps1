param(
    [string]$AgentId = "isasha_R7_Cudy-windows",
    [string]$AgentSecretsDir = "$PSScriptRoot\..\secrets\agents",
    [string]$SourceDir = "$PSScriptRoot\agent-windows",
    [string]$OutputDir = "$PSScriptRoot\..\secrets\agents",
    [string]$VersionName = "1.27",
    [int]$VersionCode = 28,
    [switch]$IncludeRuntime,
    [switch]$SkipZip
)

$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [Console]::OutputEncoding
} catch {
}

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$source = (Resolve-Path -LiteralPath $SourceDir).Path
$agentDir = Join-Path $AgentSecretsDir $AgentId
if (-not (Test-Path -LiteralPath $agentDir)) {
    throw "Agent secrets directory not found: $agentDir"
}

$requiredSecretFiles = @(
    "agent.env.ps1",
    "uswest_control_tunnel_ed25519",
    "uswest_control_tunnel_ed25519.pub"
)
foreach ($file in $requiredSecretFiles) {
    $path = Join-Path $agentDir $file
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required agent file is missing: $path"
    }
}

$stageRoot = Join-Path $root "build\agent-packages"
$stageDir = Join-Path $stageRoot $AgentId
if (Test-Path -LiteralPath $stageDir) {
    Remove-Item -LiteralPath $stageDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $stageDir | Out-Null

$sourceFiles = @(
    "agent.env.ps1.example",
    "Apply-Routes.ps1",
    "Apply-Test-Routes.ps1",
    "Check-Net.ps1",
    "Cudy-Agent.ps1",
    "Emergency-Stop-Agent.cmd",
    "Emergency-Stop-Agent.ps1",
    "Enroll-Agent.ps1",
    "Find-SingBox.ps1",
    "Get-ManagedAgentStatus.ps1",
    "Get-AgentUpdateStatus.ps1",
    "Get-AgentUiStatus.ps1",
    "Install-AgentWatchdogTask.ps1",
    "Install-AgentUi.ps1",
    "Install-UniversalAgent.ps1",
    "Install-ManagedAgentTask.ps1",
    "Install-SingBoxRuntime.ps1",
    "Invoke-AgentDiagnostics.ps1",
    "New-LokVpnConfig.ps1",
    "New-SingBoxHttpProxyConfig.ps1",
    "New-SingBoxVlessRealityConfig.ps1",
    "Register-CudyAgentInstallation.ps1",
    "README.md",
    "Restore-Direct.ps1",
    "Restart-AgentUi.ps1",
    "Run-Plan.ps1",
    "Send-AwgUapi.ps1",
    "Start-AwgTransport.ps1",
    "Start-ManagedAgent.ps1",
    "Start-SingBoxTransport.ps1",
    "Start-Tunnel.ps1",
    "Set-AgentState.ps1",
    "Stop-AwgTransport.ps1",
    "Stop-SingBoxTransport.ps1",
    "Test-AutoMode.ps1",
    "Test-ControlTransportPlan.ps1",
    "Test-ManagedRouting.ps1",
    "Test-ProdAgent.ps1",
    "Uninstall-ManagedAgentTask.ps1",
    "Uninstall-AgentWatchdogTask.ps1",
    "Uninstall-CudyAgent.ps1",
    "Update-AgentPackage.ps1",
    "Update-LokVpnConfig.ps1",
    "Update-VpnTypeProxyConfig.ps1",
    "Watch-AgentConnectivity.ps1",
    "watchdog-services.json.example"
)

foreach ($file in $sourceFiles) {
    $src = Join-Path $source $file
    if (-not (Test-Path -LiteralPath $src)) {
        throw "Source file is missing: $src"
    }
    Copy-Item -LiteralPath $src -Destination (Join-Path $stageDir $file) -Force
}

Copy-Item -LiteralPath (Join-Path $root "tools\route_agent.py") -Destination (Join-Path $stageDir "route_agent.py") -Force
Copy-Item -LiteralPath (Join-Path $agentDir "agent.env.ps1") -Destination (Join-Path $stageDir "agent.env.ps1") -Force
Copy-Item -LiteralPath (Join-Path $agentDir "uswest_control_tunnel_ed25519") -Destination (Join-Path $stageDir "uswest_control_tunnel_ed25519") -Force
Copy-Item -LiteralPath (Join-Path $agentDir "uswest_control_tunnel_ed25519.pub") -Destination (Join-Path $stageDir "uswest_control_tunnel_ed25519.pub") -Force

$versionJson = @{
    platform = "windows"
    version_name = $VersionName
    version_code = $VersionCode
} | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText((Join-Path $stageDir "agent.version.json"), $versionJson, [System.Text.UTF8Encoding]::new($false))

foreach ($optionalFile in @("aktau-awg.conf", "uswest-awg.conf", "client-awg.conf")) {
    $path = Join-Path $agentDir $optionalFile
    if (Test-Path -LiteralPath $path) {
        Copy-Item -LiteralPath $path -Destination (Join-Path $stageDir $optionalFile) -Force
    }
}

if ($IncludeRuntime) {
    $runtime = Join-Path $agentDir "runtime"
    if (-not (Test-Path -LiteralPath $runtime)) {
        throw "Runtime directory requested but not found: $runtime"
    }
    Copy-Item -LiteralPath $runtime -Destination (Join-Path $stageDir "runtime") -Recurse -Force
}

$fileCount = (Get-ChildItem -LiteralPath $stageDir -Recurse -File | Measure-Object).Count
Write-Host "Windows agent package staged: $stageDir"
Write-Host "files=$fileCount"

if (-not $SkipZip) {
    $resolvedOutputDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDir)
    New-Item -ItemType Directory -Force -Path $resolvedOutputDir | Out-Null
    $zipPath = Join-Path $resolvedOutputDir "$AgentId-prod.zip"
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    $stageItems = Get-ChildItem -LiteralPath $stageDir -Force
    if (-not $stageItems) {
        throw "Stage directory is empty: $stageDir"
    }
    Compress-Archive -LiteralPath $stageItems.FullName -DestinationPath $zipPath -Force
    $zip = Get-Item -LiteralPath $zipPath
    Write-Host "Windows agent package zip: $($zip.FullName)"
    Write-Host "bytes=$($zip.Length)"
    Write-Host "modified=$($zip.LastWriteTime.ToString('s'))"
}
