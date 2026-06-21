param(
    [string]$ProjectPath = "$PSScriptRoot\..\apps\CudyAndroidAgent\CudyAndroidAgent.csproj",
    [string]$RuntimeIdentifier = "android-arm64",
    [string]$Configuration = "Release",
    [string]$OutputDir = "$PSScriptRoot\..\build\releases",
    [string]$VersionName = "",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [Console]::OutputEncoding
} catch {
}

$project = (Resolve-Path -LiteralPath $ProjectPath).Path
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path

if (-not $VersionName) {
    [xml]$projectXml = Get-Content -LiteralPath $project
    $displayVersion = $projectXml.Project.PropertyGroup |
        ForEach-Object { $_.ApplicationDisplayVersion } |
        Where-Object { $_ } |
        Select-Object -First 1
    $VersionName = if ($displayVersion) { [string]$displayVersion } else { "dev" }
}

if (-not $SkipBuild) {
    dotnet build $project -c $Configuration -p:RuntimeIdentifier=$RuntimeIdentifier
    if ($LASTEXITCODE -ne 0) {
        throw "dotnet build failed with exit code $LASTEXITCODE"
    }
}

$projectDir = Split-Path -Parent $project
$apkPath = Join-Path $projectDir "bin\$Configuration\net10.0-android\$RuntimeIdentifier\com.nashvpn.cudyagent-Signed.apk"
if (-not (Test-Path -LiteralPath $apkPath)) {
    throw "Release APK was not found: $apkPath"
}

$dateStamp = Get-Date -Format "yyyyMMdd"
$releaseName = "NashVPN-CudyAgent-$RuntimeIdentifier-v$VersionName-$dateStamp.apk"
$resolvedOutputDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDir)
New-Item -ItemType Directory -Force -Path $resolvedOutputDir | Out-Null
$releasePath = Join-Path $resolvedOutputDir $releaseName
Copy-Item -Force -LiteralPath $apkPath -Destination $releasePath

$apk = Get-Item -LiteralPath $releasePath
$relativePath = $apk.FullName
if ($apk.FullName.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
    $relativePath = $apk.FullName.Substring($root.Length).TrimStart("\", "/")
}
Write-Host "Android release APK: $relativePath"
Write-Host "bytes=$($apk.Length)"
Write-Host "modified=$($apk.LastWriteTime.ToString('s'))"
