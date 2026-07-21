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
    if ($env:AGENT_VERSION_CODE) {
        return [int64]$env:AGENT_VERSION_CODE
    }
    if (Test-Path -LiteralPath $VersionFile) {
        try {
            $payload = Get-Content -Raw -LiteralPath $VersionFile | ConvertFrom-Json
            return [int64]($payload.version_code)
        } catch {
            return 0
        }
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
    Start-Sleep -Seconds 3
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Copy-UpdateFiles -SourceDir $StagePath
    $manifestPath = Join-Path $StagePath "agent.version.json"
    if (Test-Path -LiteralPath $manifestPath) {
        Copy-Item -LiteralPath $manifestPath -Destination $VersionFile -Force
    }
    $uiInstaller = Join-Path $PSScriptRoot "Install-AgentUi.ps1"
    if (Test-Path -LiteralPath $uiInstaller) {
        & $uiInstaller
    }
    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host "Agent update applied from $StagePath"
    exit 0
}

. "$PSScriptRoot\agent.env.ps1"
$manifest = Get-UpdateManifest
$currentCode = Read-AgentVersionCode
$latestCode = [int64]($manifest.version_code)
if (-not $Force -and $latestCode -le $currentCode) {
    Write-Host "Agent is up to date: current=$currentCode latest=$latestCode"
    exit 0
}
if (-not $manifest.download_url) {
    Write-Host "Update available but download_url is empty: current=$currentCode latest=$latestCode"
    exit 0
}

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$archivePath = Join-Path $WorkDir ("agent-update-{0}-{1}.zip" -f $Platform, $latestCode)
$downloadUrl = [string]$manifest.download_url
if ($downloadUrl.StartsWith("/")) {
    $downloadUrl = ($ControlUrl.TrimEnd("/")) + $downloadUrl
}
$downloadHeaders = @{}
if ($env:VPN_AGENT_TOKEN -and $downloadUrl.StartsWith($ControlUrl.TrimEnd("/"))) {
    $downloadHeaders["Authorization"] = "Bearer $($env:VPN_AGENT_TOKEN)"
}
Invoke-WebRequest -UseBasicParsing -Uri $downloadUrl -Headers $downloadHeaders -OutFile $archivePath -TimeoutSec 120
$stage = Expand-UpdateArchive -ArchivePath $archivePath
($manifest | ConvertTo-Json -Depth 10) | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $stage "agent.version.json")

$script = Join-Path $PSScriptRoot "Update-AgentPackage.ps1"
Start-Process -WindowStyle Hidden -FilePath "powershell.exe" -ArgumentList @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$script`"",
    "-ApplyStaged",
    "-StagePath", "`"$stage`"",
    "-TaskName", "`"$TaskName`"",
    "-VersionFile", "`"$VersionFile`""
) | Out-Null
Write-Host "Agent update downloaded and apply process started: latest=$latestCode current=$currentCode"
if ($FromAgent) {
    exit 10
}
exit 0
