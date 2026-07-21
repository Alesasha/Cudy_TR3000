param(
    [string]$OutputDir = "$PSScriptRoot\..\build\agent-updates",
    [string]$VersionName = "1.27",
    [int]$VersionCode = 28,
    [string]$LinuxVersionName = "1.30",
    [int]$LinuxVersionCode = 31,
    [string]$AndroidVersionName = "1.43",
    [int]$AndroidVersionCode = 44,
    [string]$AndroidApk = "",
    [ValidateSet("windows", "linux", "android")]
    [string[]]$Platforms = @("windows", "linux", "android")
)

$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [Console]::OutputEncoding
} catch {
}

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$resolvedOutputDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDir)
New-Item -ItemType Directory -Force -Path $resolvedOutputDir | Out-Null

function Write-VersionFile {
    param(
        [Parameter(Mandatory = $true)][string]$Platform,
        [Parameter(Mandatory = $true)][string]$ArtifactPath,
        [Parameter(Mandatory = $true)][string]$ArtifactVersionName,
        [Parameter(Mandatory = $true)][int]$ArtifactVersionCode
    )
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ArtifactPath).Hash.ToLowerInvariant()
    $content = [pscustomobject]@{
        platform = $Platform
        version_name = $ArtifactVersionName
        version_code = $ArtifactVersionCode
        sha256 = $hash
        artifact = (Split-Path -Leaf $ArtifactPath)
        built_at = (Get-Date).ToString("s")
    } | ConvertTo-Json -Depth 5
    $target = Join-Path $resolvedOutputDir "$Platform.version.json"
    [System.IO.File]::WriteAllText($target, $content, [System.Text.UTF8Encoding]::new($false))
}

function New-ZipFromItems {
    param(
        [Parameter(Mandatory = $true)][string]$StageDir,
        [Parameter(Mandatory = $true)][string]$ZipPath
    )
    if (Test-Path -LiteralPath $ZipPath) {
        Remove-Item -LiteralPath $ZipPath -Force
    }
    $items = Get-ChildItem -LiteralPath $StageDir -Force
    Compress-Archive -LiteralPath $items.FullName -DestinationPath $ZipPath -Force
}

function Build-WindowsUpdate {
    $source = Join-Path $root "tools\agent-windows"
    $stage = Join-Path $root "build\agent-updates-stage\windows"
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    $files = @(
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
    foreach ($file in $files) {
        Copy-Item -LiteralPath (Join-Path $source $file) -Destination (Join-Path $stage $file) -Force
    }
    Copy-Item -LiteralPath (Join-Path $root "tools\route_agent.py") -Destination (Join-Path $stage "route_agent.py") -Force
    $versionJson = [pscustomobject]@{ platform = "windows"; version_name = $VersionName; version_code = $VersionCode } |
        ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText((Join-Path $stage "agent.version.json"), $versionJson, [System.Text.UTF8Encoding]::new($false))
    $zipPath = Join-Path $resolvedOutputDir "windows.zip"
    New-ZipFromItems -StageDir $stage -ZipPath $zipPath
    Write-VersionFile -Platform "windows" -ArtifactPath $zipPath -ArtifactVersionName $VersionName -ArtifactVersionCode $VersionCode
}

function Build-LinuxUpdate {
    $source = Join-Path $root "tools\agent-linux"
    $stage = Join-Path $root "build\agent-updates-stage\linux"
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    $files = @(
        "QUICKSTART-RU.md",
        "README.md",
        "agent_off.sh",
        "agent_on.sh",
        "agent.env.example",
        "cudy_agent_ui.sh",
        "cudy_agent_ui.py",
        "fresh_install_from_zip.sh",
        "install_singbox_runtime.sh",
        "install_desktop_shortcuts.sh",
        "install_systemd.sh",
        "managed_agent.sh",
        "one_click_install.sh",
        "open_user_ui.sh",
        "restore_direct.sh",
        "run_diagnostics.sh",
        "run_speed_tests.sh",
        "start_singbox_transport.sh",
        "start_tunnel.sh",
        "status.sh",
        "stop_singbox_transport.sh",
        "test_prod_agent.sh",
        "uninstall_systemd.sh",
        "update_agent.sh",
        "watch_agent_connectivity.py",
        "write_transport_plan.py"
    )
    foreach ($file in $files) {
        Copy-Item -LiteralPath (Join-Path $source $file) -Destination (Join-Path $stage $file) -Force
    }
    Copy-Item -LiteralPath (Join-Path $root "tools\route_agent.py") -Destination (Join-Path $stage "route_agent.py") -Force
    $versionJson = [pscustomobject]@{ platform = "linux"; version_name = $LinuxVersionName; version_code = $LinuxVersionCode } |
        ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText((Join-Path $stage "agent.version.json"), $versionJson, [System.Text.UTF8Encoding]::new($false))
    $zipPath = Join-Path $resolvedOutputDir "linux.zip"
    New-ZipFromItems -StageDir $stage -ZipPath $zipPath
    Write-VersionFile -Platform "linux" -ArtifactPath $zipPath -ArtifactVersionName $LinuxVersionName -ArtifactVersionCode $LinuxVersionCode
}

function Build-AndroidUpdate {
    $requestedApk = $AndroidApk
    if (-not $requestedApk) {
        $releaseDir = Join-Path $root "build\releases"
        $requestedApk = Get-ChildItem -LiteralPath $releaseDir -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "NashVPN-CudyAgent-android-arm64-v$AndroidVersionName-*.apk" } |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1 -ExpandProperty FullName
        if (-not $requestedApk) {
            throw "Android release APK v$AndroidVersionName was not found in $releaseDir. Run Build-AndroidAgentRelease.ps1 first."
        }
    }
    $apkPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($requestedApk)
    if (-not (Test-Path -LiteralPath $apkPath)) {
        throw "Android APK not found: $apkPath"
    }
    $target = Join-Path $resolvedOutputDir "android.apk"
    Copy-Item -LiteralPath $apkPath -Destination $target -Force
    Write-VersionFile -Platform "android" -ArtifactPath $target -ArtifactVersionName $AndroidVersionName -ArtifactVersionCode $AndroidVersionCode
}

$selectedPlatforms = @($Platforms | ForEach-Object { $_.ToLowerInvariant() } | Select-Object -Unique)
if ($selectedPlatforms -contains "windows") { Build-WindowsUpdate }
if ($selectedPlatforms -contains "linux") { Build-LinuxUpdate }
if ($selectedPlatforms -contains "android") { Build-AndroidUpdate }

Get-ChildItem -LiteralPath $resolvedOutputDir -File | Sort-Object Name | ForEach-Object {
    Write-Host "$($_.Name) bytes=$($_.Length) modified=$($_.LastWriteTime.ToString('s'))"
}
