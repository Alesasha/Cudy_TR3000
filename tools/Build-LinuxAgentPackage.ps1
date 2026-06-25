param(
    [string]$AgentId = "DC_via_Cudy-linux",
    [string]$AgentSecretsDir = "$PSScriptRoot\..\secrets\agents",
    [string]$SourceDir = "$PSScriptRoot\agent-linux",
    [string]$OutputDir = "$PSScriptRoot\..\secrets\agents",
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
    "agent.env",
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
    "QUICKSTART-RU.md",
    "README.md",
    "agent.env.example",
    "install_singbox_runtime.sh",
    "install_systemd.sh",
    "managed_agent.sh",
    "one_click_install.sh",
    "restore_direct.sh",
    "start_singbox_transport.sh",
    "start_tunnel.sh",
    "status.sh",
    "stop_singbox_transport.sh",
    "test_prod_agent.sh",
    "uninstall_systemd.sh",
    "write_transport_plan.py"
)

foreach ($file in $sourceFiles) {
    $src = Join-Path $source $file
    if (-not (Test-Path -LiteralPath $src)) {
        throw "Source file is missing: $src"
    }
    Copy-Item -LiteralPath $src -Destination (Join-Path $stageDir $file) -Force
}

Copy-Item -LiteralPath (Join-Path $root "tools\route_agent.py") -Destination (Join-Path $stageDir "route_agent.py") -Force
Copy-Item -LiteralPath (Join-Path $agentDir "agent.env") -Destination (Join-Path $stageDir "agent.env") -Force
Copy-Item -LiteralPath (Join-Path $agentDir "uswest_control_tunnel_ed25519") -Destination (Join-Path $stageDir "uswest_control_tunnel_ed25519") -Force
Copy-Item -LiteralPath (Join-Path $agentDir "uswest_control_tunnel_ed25519.pub") -Destination (Join-Path $stageDir "uswest_control_tunnel_ed25519.pub") -Force

if ($IncludeRuntime) {
    $runtime = Join-Path $agentDir "runtime"
    if (-not (Test-Path -LiteralPath $runtime)) {
        throw "Runtime directory requested but not found: $runtime"
    }
    Copy-Item -LiteralPath $runtime -Destination (Join-Path $stageDir "runtime") -Recurse -Force
}

$fileCount = (Get-ChildItem -LiteralPath $stageDir -Recurse -File | Measure-Object).Count
Write-Host "Linux agent package staged: $stageDir"
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
    Write-Host "Linux agent package zip: $($zip.FullName)"
    Write-Host "bytes=$($zip.Length)"
    Write-Host "modified=$($zip.LastWriteTime.ToString('s'))"
}
