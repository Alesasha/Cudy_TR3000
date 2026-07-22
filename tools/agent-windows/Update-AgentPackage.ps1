param(
    [string]$ControlUrl = "http://127.0.0.1:18765",
    [string]$Platform = "windows",
    [string]$TaskName = "Cudy Managed Route Agent",
    [string]$VersionFile = "$PSScriptRoot\agent.version.json",
    [string]$WorkDir = "$PSScriptRoot\updates",
    [string]$StagePath = "",
    [switch]$FromAgent,
    [switch]$ApplyStaged,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Read-AgentVersionCode {
    if (Test-Path -LiteralPath $VersionFile) {
        try {
            $payload = Get-Content -Raw -LiteralPath $VersionFile | ConvertFrom-Json
            return [int64]($payload.version_code)
        } catch {
            return 0
        }
    }
    if ($env:AGENT_VERSION_CODE) {
        return [int64]$env:AGENT_VERSION_CODE
    }
    return 0
}

function Write-AgentVersion {
    param($Manifest)
    $payload = [pscustomobject]@{
        platform = $Platform
        version_name = [string]$Manifest.version_name
        version_code = [int64]$Manifest.version_code
        updated_at = (Get-Date).ToString("s")
    }
    $payload | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath $VersionFile
}

function Invoke-ControlJson {
    param([string]$Path)
    $headers = @{}
    if ($env:VPN_AGENT_TOKEN) {
        $headers["Authorization"] = "Bearer $($env:VPN_AGENT_TOKEN)"
    }
    return Invoke-RestMethod -UseBasicParsing -Uri (($ControlUrl.TrimEnd("/")) + $Path) -Headers $headers -TimeoutSec 20
}

function Get-UpdateManifest {
    Invoke-ControlJson -Path "/api/agent/app-version?platform=$Platform"
}

function Expand-UpdateArchive {
    param([string]$ArchivePath)
    $stageRoot = Join-Path $WorkDir "stage"
    if (Test-Path -LiteralPath $stageRoot) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $stageRoot | Out-Null
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $stageRoot -Force
    $children = @(Get-ChildItem -LiteralPath $stageRoot -Force)
    if ($children.Count -eq 1 -and $children[0].PSIsContainer) {
        return $children[0].FullName
    }
    return $stageRoot
}

function Copy-UpdateFiles {
    param([string]$SourceDir)
    $preserve = @(
        "agent.env.ps1",
        "uswest_control_tunnel_ed25519",
        "uswest_control_tunnel_ed25519.pub",
        "managed-agent.log",
        "managed-agent.log.1"
    )
    $preserveDirs = @("logs", "run", "transports", "updates")
    Get-ChildItem -LiteralPath $SourceDir -Force | ForEach-Object {
        if ($_.Name -in $preserve -or $_.Name -in $preserveDirs) {
            return
        }
        if ($_.Name -like "*.conf") {
            return
        }
        $destination = Join-Path $PSScriptRoot $_.Name
        if (Test-Path -LiteralPath $destination) {
            Remove-Item -LiteralPath $destination -Recurse -Force
        }
        Copy-Item -LiteralPath $_.FullName -Destination $destination -Recurse -Force
    }
}

if ($ApplyStaged) {
    if (-not $StagePath -or -not (Test-Path -LiteralPath $StagePath)) {
        throw "StagePath is required for -ApplyStaged."
    }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Copy-UpdateFiles -SourceDir $StagePath
    $manifestPath = Join-Path $StagePath "agent.version.json"
    if (Test-Path -LiteralPath $manifestPath) {
        Copy-Item -LiteralPath $manifestPath -Destination $VersionFile -Force
    }
    $standardInstallDir = [IO.Path]::GetFullPath((Join-Path $env:ProgramFiles "Cudy Agent")).TrimEnd('\')
    $currentInstallDir = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
    if (-not [string]::Equals($currentInstallDir, $standardInstallDir, [StringComparison]::OrdinalIgnoreCase)) {
        & (Join-Path $PSScriptRoot "Install-UniversalAgent.ps1") -StartNow:$true
        Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "Agent update applied and migrated to $standardInstallDir"
        exit 0
    }
    $uiInstaller = Join-Path $PSScriptRoot "Install-AgentUi.ps1"
    if (Test-Path -LiteralPath $uiInstaller) {
        & $uiInstaller
    }
    & (Join-Path $PSScriptRoot "Register-CudyAgentInstallation.ps1")
    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Agent update applied from $StagePath"
    exit 0
}

. "$PSScriptRoot\agent.env.ps1"
$manifest = Get-UpdateManifest
$currentCode = Read-AgentVersionCode
$latestCode = [int64]($manifest.version_code)
if (-not $Force -and $latestCode -le $currentCode) {
    Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Agent is up to date: current=$currentCode latest=$latestCode"
    exit 0
}
if (-not $manifest.download_url) {
    Write-Host "Update available but download_url is empty: current=$currentCode latest=$latestCode"
    exit 0
}

$downloadUrl = [string]$manifest.download_url
if ($downloadUrl.StartsWith("/")) {
    $downloadUrl = ($ControlUrl.TrimEnd("/")) + $downloadUrl
}
$stageRoot = Join-Path $WorkDir "stage"
$stagedManifestPath = Join-Path $stageRoot "agent.version.json"
$stagedCode = 0
if (Test-Path -LiteralPath $stagedManifestPath) {
    try {
        $stagedCode = [int64]((Get-Content -Raw -LiteralPath $stagedManifestPath | ConvertFrom-Json).version_code)
    } catch {
        $stagedCode = 0
    }
}
if (-not $Force -and $stagedCode -eq $latestCode) {
    if ($FromAgent) {
        Write-Host "Agent update is downloaded and waiting for user approval: current=$currentCode latest=$latestCode"
        exit 0
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath `
        -ApplyStaged -StagePath $stageRoot -TaskName $TaskName -VersionFile $VersionFile
    exit $LASTEXITCODE
}

Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$archivePath = Join-Path $WorkDir ("agent-update-{0}-{1}.zip" -f $Platform, $latestCode)
$downloadHeaders = @{}
if ($env:VPN_AGENT_TOKEN -and $downloadUrl.StartsWith($ControlUrl.TrimEnd("/"))) {
    $downloadHeaders["Authorization"] = "Bearer $($env:VPN_AGENT_TOKEN)"
}
Invoke-WebRequest -UseBasicParsing -Uri $downloadUrl -Headers $downloadHeaders -OutFile $archivePath -TimeoutSec 120
$expectedSha256 = ([string]$manifest.sha256).Trim().ToLowerInvariant()
if ($expectedSha256) {
    $actualSha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha256 -ne $expectedSha256) {
        throw "Downloaded update checksum mismatch."
    }
}
$stage = Expand-UpdateArchive -ArchivePath $archivePath
($manifest | ConvertTo-Json -Depth 10) | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $stage "agent.version.json")

if ($FromAgent) {
    Write-Host "Agent update downloaded and verified; waiting for user approval: current=$currentCode latest=$latestCode"
    exit 0
}

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath `
    -ApplyStaged -StagePath $stage -TaskName $TaskName -VersionFile $VersionFile
exit $LASTEXITCODE
